# ha-pvnode

Home Assistant integration for the PV forecast service [pvnode](https://pvnode.com).
Uses the **v2 API** exclusively.

## Installation
### Via HACS (recommended)
HACS → Integrations → "⋮" menu → "Custom repositories".
Add this repository (darwindaume/ha-pvnode) as a repository of type "Integration".
Install "pvnode" and restart Home Assistant.

### Manual
Copy the custom_components/pvnode folder into the custom_components directory of your Home Assistant configuration.
Restart Home Assistant.
Go to Settings → Devices & Services → Add Integration → "pvnode".

## Setup

1. Create an API key in your pvnode account.
2. **Settings → Devices & Services → Add Integration → pvnode**.
3. Enter the key, pick your site from the list. Done.

There is nothing else to configure. Forecast horizon and refresh cadence follow from your
pvnode plan and are steered by the API itself — the integration asks exactly when a new
computation is available, and survives a restart without a single extra request.

> **Changed plan?** Forecast horizon and uncertainty band adjust themselves on the next
> refresh — no reload needed. The band sensors appear after the first forecast update, not
> immediately after setup.

If you don't want to wait, press **"Refresh forecast now"** on the pvnode device. The
button fetches a new forecast straight away. Home Assistant's own *Reload* does not help
here: it falls back to the stored forecast and deliberately avoids a request while that
one is still current.

Every press counts against your request quota, even when the server returns the same
computation. After a plan change it is different — the server-side cache is invalidated
then, so a genuine recomputation happens.

## Sensors

Each site becomes a device, each roof surface its own sub-device.

### Site

| Sensor | Unit | Meaning |
|---|---|---|
| Power forecast now | W | forecast for the current 15-minute slot |
| Power forecast in 30 minutes / 1 hour | W | for short-term automations |
| Peak power today / tomorrow | W | daily maximum |
| Time of peak power today / tomorrow | time | when the maximum occurs |
| Clearsky power now | W | reference without clouds |
| Energy forecast today / tomorrow / in N days | kWh | daily total, straight from pvnode |
| Clearsky energy today | kWh | daily total without clouds |
| Remaining yield today | kWh | what is still to come from now on |
| Energy this / next hour | Wh | short-term window |
| Temperature forecast, weather code | °C, WMO | site-wide |

The number of day sensors follows your plan.

**Disabled by default**, individually enabled in the entity registry: wind speed, relative
humidity, precipitation, snow water equivalent, and global, diffuse and beam irradiance.

**Only on a suitable plan:** power and energy forecast as a `(min)`/`(max)` pair.

### Roof surface

Power forecast now, energy forecast today and tomorrow, plus tilted irradiance before and
after shading (disabled).

Daily energy per roof surface is **derived** — pvnode reports daily totals for the site as
a whole only. For accounting, the site-level value is the authoritative one.

### Diagnostics

Forecast computed at, next forecast update (with `included` and `available` as
attributes), requests remaining. The request counter is **account-wide**, not per site.

## Energy Dashboard

pvnode can supply the forecast curve for a solar panel in the Energy Dashboard.

**A real production meter is required.** For "Solar production" the Energy Dashboard needs
an accumulating energy sensor (`state_class: total` or `total_increasing`) — typically
from your inverter. A forecast is not a meter and cannot fill that field.

1. **Settings → Dashboards → Energy**
2. Under *Solar panels*, add your production sensor.
3. Under *Forecast*, select **pvnode**.

The forecast then appears as a curve above actual production. It is fetched live on every
page load and not historised — looking back, you always see the current forecast, not the
one from back then.

## Charts

Home Assistant ships no built-in card for forecast curves. The usual route is the
[ApexCharts card](https://github.com/RomRider/apexcharts-card) from HACS.

Every power sensor carries its full time series in the `forecast` attribute:

```yaml
forecast:
  - datetime: "2026-08-12T05:00:00+00:00"
    watts: 0
  - datetime: "2026-08-12T05:15:00+00:00"
    watts: 12
```

Replace `sensor.my_site_...` in the example below with your own entity IDs (find them
under *Developer Tools → States*, filter `pvnode`).

### Forecast curve with clearsky reference

The gap between the two lines is the loss to cloud cover.

```yaml
type: custom:apexcharts-card
graph_span: 2d
span:
  start: day
now:
  show: true
  label: Now
header:
  show: true
  title: PV Forecast
series:
  - entity: sensor.my_site_clearsky_power_now
    name: Clearsky
    type: area
    opacity: 0.15
    stroke_width: 1
    color: "#cccccc"
    data_generator: |
      return entity.attributes.forecast.map((e) => {
        return [new Date(e.datetime).getTime(), e.watts_clearsky];
      });
  - entity: sensor.my_site_power_forecast_now
    name: Forecast
    type: area
    opacity: 0.4
    stroke_width: 2
    data_generator: |
      return entity.attributes.forecast.map((e) => {
        return [new Date(e.datetime).getTime(), e.watts];
      });
```

## Migrating from Forecast.Solar

Remove Forecast.Solar first, then set up pvnode and adjust your automations.

The left column shows the **entity ID** Forecast.Solar assigns permanently — identical in
every installation, so you can search your YAML for it. The right column shows the
**sensor name** in pvnode. A fixed entity ID cannot be listed there: it is built from your
site name, so for "House North" it becomes
`sensor.house_north_energy_forecast_today`.

| Forecast.Solar — entity ID | pvnode — sensor name |
|---|---|
| `sensor.energy_production_today` | Energy forecast today |
| `sensor.energy_production_tomorrow` | Energy forecast tomorrow |
| `sensor.energy_production_today_remaining` | Remaining yield today |
| `sensor.power_production_now` | Power forecast now |
| `sensor.power_production_next_hour` | Power forecast in 1 hour |
| `sensor.power_highest_peak_time_today` | Time of peak power today |
| `sensor.power_highest_peak_time_tomorrow` | Time of peak power tomorrow |
| `sensor.energy_current_hour` | Energy this hour |
| `sensor.energy_next_hour` | Energy next hour |

Your actual IDs are under *Developer Tools → States*, filter `pvnode`.

On top of that, pvnode gives you values with no previous equivalent: clearsky power and
energy as a cloudless reference, weather and irradiance data, separate sensors per roof
surface and — depending on your plan — the uncertainty band.

The forecast source in the Energy Dashboard has to be selected once more: it hangs off the
config entry, not the entities.

## Development

To try it out, copy `custom_components/pvnode` into the `config/custom_components/`
directory of a Home Assistant instance and restart it.

### Tests

```bash
pip install -r requirements_test.txt
pytest
```

Home Assistant's test harness imports `fcntl` and therefore only runs on Linux and macOS.
On Windows it needs a container or WSL with Python 3.13+.

The suite runs against a recorded API response in `tests/fixtures/forecast.json`. Its
dates are shifted to the current day on load — otherwise every test checking "today" would
start failing the day after it was recorded.

Always build timestamps in tests from `datetime.now(ZoneInfo(...))`, never from a fixed
offset like `+02:00`. Across daylight-saving transitions the two behave differently, and
then you are testing something other than what runs.

## Origin

Based on [`patricknitsch/ha-pvnode`](https://github.com/patricknitsch/ha-pvnode) by
Patrick Nitsch (MIT). Adopted from it, among other things: the device and entity
structure, the cleanup of roof surfaces that disappear, and the test setup. The licence
and the original copyright notice are retained.
