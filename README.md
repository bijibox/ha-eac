# EAC Cyprus — Home Assistant integration

Home Assistant integration for the EAC (Electricity Authority of Cyprus) Distribution Web Portal at `meterreading-dso.eac.com.cy`.

It pulls your electricity-meter readings into Home Assistant — the full history into the Energy Dashboard, plus live "latest known value" sensors and per-meter diagnostics. Built on the [`eac-dso-portal`](https://github.com/bijibox/eac-dso-portal) Python client (also on [PyPI](https://pypi.org/project/eac-dso-portal/)).

## What you get

**One device per meter.** Each physical meter on your account is its own HA device, named by its serial and parented to the service point (the permanent supply contract at your address). When the DSO swaps the meter, the old device stays — with its final reading and full history — and the new meter appears automatically.

Per meter you get:

- **Per-channel "last value" sensors** — the most recent reading Home Assistant has for each channel:
  - `Energy total (24h)`, `Energy peak/off-peak tariff`, `Energy exported` — cumulative kWh (glance value; the *history* lives in statistics, see below).
  - `Power import/export (30-min average)` — the 30-minute load profile re-projected to average power (kW).
  - Reactive / apparent energy channels — created but disabled by default.
- **Diagnostics** — `Data measured at` (when the reading was taken), `Data received at` (when HA fetched it), `Data lag` (the age of the newest reading), and a `Data stale` problem sensor.

## "Now" vs "history": two separate things

The portal publishes with a **2–3 day lag**. Home Assistant's live sensor state is, by design, always "the value right now" — you can't backdate it — so a cumulative sensor showing a 3-day-old reading as "now" is a poor fit, and letting Home Assistant auto-compute long-term statistics off it would misdate your consumption. (This is exactly why Home Assistant's own utility integrations, like `opower`, publish delayed data as statistics rather than as a live sensor.)

So the integration splits the picture in two:

- **Sensors answer "what does HA know right now"** — the latest reading and the diagnostics above. Good for glance cards and automations. Cumulative kWh sensors deliberately carry **no `state_class`**, so Home Assistant does not auto-record misdated statistics for them.
- **External statistics answer "what actually happened, and when"** — the correct-dated long-term history, written straight to the statistics engine with the real reading dates. This is what the **Energy Dashboard** uses.

### Where to find the history

The statistics are named from the meter serial, e.g. `1281890 Energy imported (30-min)`. They are not entities (that is inherent to delayed-data statistics), so you view them via:

- **Energy Dashboard** → *Grid consumption* → pick `<serial> Energy imported (30-min)`. Use `<serial> Energy exported (30-min)` under **Return to grid**. Keep the retired meter as its own historical source. **Never add the total together with its peak/off-peak tariff components.**
- A **Statistics graph** card on any dashboard → pick the same series.
- **Developer Tools → Statistics** → lists them all.

## Load the full history

The first successful poll backfills each energy channel from its meter installation date. To explicitly refresh everything the portal has — the current meter back to its install date, **and** any retired meters (e.g. an old meter's years of readings) — run the action once:

**Developer Tools → Actions → `eac_cyprus.import_full_history` → Run.**

It returns a per-channel summary and posts a notification when done. Both import/export load profiles and sparse cumulative channels are included. Failed requests are reported as failures. Polling and manual imports share a lock; repeats and restarts are idempotent.

## Configuration

Settings → Devices & Services → the EAC tile → three-dot menu → **Configure**:

- **Polling interval** — 30 min … 24 h (default 6 h). The portal updates daily, so more often rarely helps.
- **History window (days)** — how far back each poll fetches (default 14). This is the correction window. Long outages are caught up automatically; a complete reconciliation runs every seven days to pick up older corrections.
- **Data-stale threshold (days)** — how long without a fresh reading before `Data stale` flags (default 4, i.e. a bit beyond the normal 2–3 day lag).

## Install

Requires Home Assistant 2026.1 or later. This fork pins its companion library to an immutable GitHub commit and archive hash; it does not depend on an unpublished PyPI release.

### HACS

Add this repository as a **Custom repository** (HACS → three-dot menu → Custom repositories → `https://github.com/bijibox/ha-eac`, category *Integration*), then install "EAC Distribution Web Portal" and restart Home Assistant.

### Manual

1. Copy `custom_components/eac_cyprus/` into your HA config's `custom_components/` folder (e.g. via the Samba add-on at `\\<HA-IP>\config\custom_components\`).
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → search **EAC Distribution Web Portal**.
4. Enter the email and password you use at `meterreading-dso.eac.com.cy`.

## Design notes

**Device = meter.** A service point (12-digit `spId`) is the permanent supply contract at an address; meters are the hardware and get swapped over time. Modelling each meter as its own device (parented to the service point via `via_device`) means a retired meter keeps its own history and final reading instead of vanishing or getting tangled with the replacement.

**Meter swaps.** Every physical meter has separate statistics. Once the replacement meter's full profile is available, it is added to an already configured EAC grid connection. The retired meter stays selected separately. Subsequent manual removal of the new source is respected.

**Precision and corrections.** `KWH-30MIN-LP-IMP` and `KWH-30MIN-LP-EXP` use the interval `value` in kWh, never average power or a rounded daily register. Decimal arithmetic builds hourly cumulative sums to 0.001 kWh. Original observations are persisted in `.storage/eac_cyprus.<entry_id>.history`; include this file in backups. A corrected interval replaces its old value and rebuilds later sums. A zero baseline before the first interval keeps that interval visible in Energy. Missing intervals are not estimated; a flat cumulative sum across a gap represents only the consumption actually reported. Old meters with sparse registers get only their observed deltas; the first register reading is a baseline, not consumption.

**Time.** Portal timestamps without offsets use `Asia/Nicosia`; explicit offsets are respected. A profile timestamp is the **end** of its 30-minute interval. This was checked against the portal's official one-day XLSX report: 48 records, 00:30 through the following 00:00. UTC aggregation preserves the 23/25-hour DST days. Repeated autumn wall times are resolved before deduplication, including equal-valued intervals. Incomplete ambiguous timestamps fail the affected request rather than silently assigning a fold. Pages include extra whole days on both sides because the portal rounds query bounds and shifts them in summer; results are clipped to the requested timestamp range after normalization.

**Upgrade from v0.3.0.** Once the initial backfill succeeds, already selected daily/tariff series are replaced with precise import/export statistics. Sparse retired sources migrate to new per-meter register statistics with a zero initial baseline. New IDs avoid mixing v0.3.0's naive-as-UTC/lifetime rows with corrected rows. The old database series are retained but deselected. Existing price and cost settings on the retained source are preserved. Two-tariff pricing may need reconfiguration because the aggregate profile does not identify individual tariffs.

**Display.** Energy sensors suggest two decimal places; Home Assistant's entity display settings can show three. External statistics retain three decimal places independently of frontend formatting. Set Home Assistant's timezone to `Asia/Nicosia` for calendar-day/month comparisons with the portal.

**No `state_class` on cumulative sensors.** With a 2–3 day publish lag, Home Assistant's auto-recorder would compute statistics off the "latest known value" and misdate consumption. Instead there is no `state_class` (nothing is auto-computed) and the real long-term series is written as external statistics with correct dates — the same approach Home Assistant core uses for delayed utility data.

## Development

```sh
uv venv --python 3.13
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/pytest tests
```

Acceptance tests exercise Home Assistant's SQLite recorder, day/month change queries, 23/25-hour days, corrections, replay, restart, sparse-register baselines, meter replacement and Energy source migration. A read-only validation on 7,940 live portal intervals produced 173 daily/monthly comparisons with a maximum difference of **0.000 kWh**. Portal gaps remain gaps; the integration cannot reconstruct observations absent from the source.


Depends on the [`eac-dso-portal`](https://github.com/bijibox/eac-dso-portal) library — bug reports about HTTP/parsing belong there; HA-side issues (sensors, config flow, devices, statistics) belong here. `scripts/deploy_smb.sh` pushes the integration to a Samba share for local testing (connection details in a gitignored `.env`).

## License

MIT
