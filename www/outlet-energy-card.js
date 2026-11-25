const outletColors = ["#4c9ae8", "#46ad92", "#a889e0", "#e0aa4a"];
const hour = 3_600_000;

function escapeHtml(value) {
  const escapes = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };
  return String(value).replace(/[&<>"']/g, (character) => escapes[character]);
}

function timestamp(value) {
  return typeof value === "number" ? value : Date.parse(value);
}

function toBuckets(meters, statistics, stop) {
  const buckets = new Map();
  meters.forEach(({ entity }, index) => {
    for (const row of statistics[entity] ?? []) {
      const start = timestamp(row.start);
      if (!Number.isFinite(start) || start >= stop) continue;
      if (!buckets.has(start)) {
        buckets.set(start, {
          start,
          end: timestamp(row.end),
          changes: Array(meters.length).fill(null),
        });
      }
      if (Number.isFinite(row.change) && row.change >= 0) {
        buckets.get(start).changes[index] = row.change;
      }
    }
  });

  const rows = [...buckets.values()].sort(
    (first, second) => first.start - second.start,
  );
  return rows.map((bucket, index) => ({
    ...bucket,
    total: bucket.changes.every(Number.isFinite)
      ? bucket.changes.reduce((sum, value) => sum + value, 0)
      : null,
    gapBefore: index > 0 && bucket.start > rows[index - 1].end,
  }));
}

class OutletEnergyCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.revision = 0;
  }

  setConfig(config) {
    if (
      !config.collection_key?.startsWith("energy_") ||
      !config.entities?.length
    ) {
      throw new Error(
        "Outlet energy requires an energy date selector and at least one energy meter.",
      );
    }
    this.detach();
    this.config = config;
    this.result = undefined;
    this.render();
    this.bind();
  }

  set hass(hass) {
    if (this.connection !== hass.connection) {
      this.detach();
      this.connection = hass.connection;
    }
    this.homeAssistant = hass;
    this.bind();
  }

  connectedCallback() {
    this.bind();
  }

  disconnectedCallback() {
    this.detach();
  }

  detach() {
    this.revision++;
    this.unsubscribe?.();
    this.unsubscribe = undefined;
    if (this.retry) clearTimeout(this.retry);
    this.retry = undefined;
  }

  bind() {
    if (
      !this.isConnected ||
      !this.config ||
      !this.connection ||
      this.unsubscribe ||
      this.retry
    )
      return;
    // Home Assistant's energy date selector stores a collection on the connection.
    const collection = this.connection[`_${this.config.collection_key}`];
    if (!collection) {
      this.retry = setTimeout(() => {
        this.retry = undefined;
        this.bind();
      }, 500);
      return;
    }
    this.unsubscribe = collection.subscribe((selection) =>
      this.load(selection),
    );
  }

  async load(selection) {
    const revision = ++this.revision;
    const { start, end } = selection;
    const stop = end ?? new Date();
    const days = (stop - start) / (24 * hour);
    const period = days >= 100 ? "month" : days > 2 ? "day" : "hour";
    try {
      const statisticIds = this.config.entities.map(({ entity }) => entity);
      const metadata = await this.homeAssistant.callWS({
        type: "recorder/get_statistics_metadata",
        statistic_ids: statisticIds,
      });
      if (this.revision !== revision) return;
      if (
        !statisticIds.every((id) =>
          metadata.some(
            (meter) =>
              meter.statistic_id === id && meter.unit_class === "energy",
          ),
        )
      ) {
        throw new Error("Energy meter statistics unavailable.");
      }
      const statistics = await this.homeAssistant.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: stop.toISOString(),
        statistic_ids: statisticIds,
        period,
        units: { energy: "Wh" },
        types: ["change"],
      });
      if (this.revision !== revision) return;
      this.result = {
        rows: toBuckets(this.config.entities, statistics, stop.getTime()),
        period,
      };
    } catch {
      if (this.revision !== revision) return;
      this.result = { error: "Energy history unavailable." };
    }
    this.render();
  }

  render() {
    if (!this.config) return;
    const { title = "Energy used", entities } = this.config;
    const { rows, period, error } = this.result ?? {};
    const locale = this.homeAssistant?.locale?.language;
    const displayNumber = new Intl.NumberFormat(locale, {
      maximumFractionDigits: 0,
    });
    const formatWh = (value) => `${displayNumber.format(value)} Wh`;
    const timeZone = this.homeAssistant?.config?.time_zone;
    const longDate = new Intl.DateTimeFormat(locale, {
      timeZone,
      month: "short",
      day: "numeric",
      ...(period === "hour" ? { hour: "numeric", minute: "2-digit" } : {}),
    });
    const shortDate = new Intl.DateTimeFormat(locale, {
      timeZone,
      ...(period === "hour"
        ? { hour: "numeric" }
        : { month: "short", day: "numeric" }),
    });
    const complete = rows?.filter(({ total }) => total !== null) ?? [];
    const recordedTotal = complete.reduce(
      (sum, bucket) => sum + bucket.total,
      0,
    );
    const highest = complete.reduce(
      (peak, bucket) => Math.max(peak, bucket.total),
      0,
    );
    const incomplete = rows?.some(
      ({ total, gapBefore }) => total === null || gapBefore,
    );
    const tickEvery = Math.max(1, Math.ceil((rows?.length ?? 0) / 5));
    const legend =
      entities.length > 1
        ? entities
            .map(
              ({ name }, index) =>
                `<span><i style="background:${outletColors[index % outletColors.length]}"></i>${escapeHtml(name)}</span>`,
            )
            .join("")
        : "";
    const bars = rows
      ?.map(({ start, total, changes, gapBefore }, index) => {
        const label = longDate.format(start);
        const detail =
          total === null
            ? `${label} · Incomplete readings`
            : `${label} · ${formatWh(total)}${
                entities.length > 1
                  ? " · " +
                    changes
                      .map(
                        (value, offset) =>
                          `${entities[offset].name} ${formatWh(value)}`,
                      )
                      .join(", ")
                  : ""
              }`;
        const segments = changes
          .map(
            (value, offset) =>
              `<span style="flex-grow:${value};background:${outletColors[offset % outletColors.length]}"></span>`,
          )
          .reverse()
          .join("");
        const tick = index % tickEvery === 0 ? shortDate.format(start) : "";
        return `${gapBefore ? '<div class="empty" aria-label="Gap in recordings">–</div>' : ""}
        <button type="button" class="bucket" aria-label="${escapeHtml(detail)}" title="${escapeHtml(detail)}">
          <span class="bar-area">${
            total === null
              ? "–"
              : `<span class="stack" style="height:${highest ? (total / highest) * 100 : 0}%">${segments}</span>`
          }</span><span class="tick">${escapeHtml(tick)}</span>
        </button>`;
      })
      .join("");
    let body =
      error ??
      (rows
        ? "No complete readings for this period."
        : "Loading energy history…");
    if (complete.length) {
      body = `<div class="chart">
        <div class="scale"><span>${escapeHtml(formatWh(highest))}</span><span>0</span></div>
        <div class="plot">${bars}</div>
      </div>
      ${legend ? `<div class="legend">${legend}</div>` : ""}
      <div class="details" role="status">${incomplete ? "Gaps in recorded energy" : ""}</div>`;
    } else {
      body = `<div class="details">${escapeHtml(body)}</div>`;
    }
    this.shadowRoot.innerHTML = `<style>
      :host { display: block; }
      ha-card { padding: 16px; }
      header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 14px; }
      header span { font-weight: 600; }
      header strong { font-size: var(--ha-font-size-xl, 20px); }
      .chart { display: flex; align-items: stretch; gap: 6px; }
      .scale { padding-bottom: 21px; display: flex; flex-direction: column; justify-content: space-between; }
      .scale, .tick, .details { color: var(--secondary-text-color); font-size: 11px; }
      .plot { display: flex; flex: 1; min-width: 0; gap: 2px; height: 147px; }
      .bucket { appearance: none; border: 0; padding: 0; background: transparent; color: inherit; flex: 1; min-width: 0; cursor: pointer; }
      .bar-area { display: flex; height: 126px; align-items: end; justify-content: center; border-bottom: 1px solid var(--divider-color); }
      .stack { display: flex; flex-direction: column; width: 100%; max-width: 32px; border-radius: 2px 2px 0 0; overflow: hidden; }
      .stack span { flex-basis: 0; }
      .bucket:hover, .bucket:focus-visible { background: var(--secondary-background-color); }
      .tick { display: block; height: 20px; line-height: 20px; white-space: nowrap; }
      .legend { display: flex; gap: 4px 12px; flex-wrap: wrap; font-size: 12px; }
      .legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-inline-end: 4px; }
      .details { min-height: 15px; margin-top: 6px; }
    </style><ha-card>
      <header><span>${escapeHtml(title)}</span>${
        complete.length
          ? `<strong>${incomplete ? "Recorded " : ""}${escapeHtml(formatWh(recordedTotal))}</strong>`
          : ""
      }</header>
      ${body}
    </ha-card>`;
    const details = this.shadowRoot.querySelector(".details");
    for (const bar of this.shadowRoot.querySelectorAll(".bucket")) {
      const showDetails = () => {
        details.textContent = bar.getAttribute("aria-label");
      };
      bar.addEventListener("pointerenter", showDetails);
      bar.addEventListener("focus", showDetails);
      bar.addEventListener("click", showDetails);
    }
  }
}

customElements.define("outlet-energy-card", OutletEnergyCard);
