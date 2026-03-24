# Plant IoT Dashboard

Autonomous plant watering system using two Heltec LoRa32 V3 nodes (ESP32-S3 + SX1262). Node 1 monitors soil moisture, temperature, humidity and light intensity; Node 2 controls the pump. Data is logged every 30 minutes to SD card over a 32-day period (13 Feb – 17 Mar 2026). The dashboard visualises the full dataset and a Ridge regression model that predicts soil drying rate from environmental inputs.

## Repository structure

```
Dashboard_IoT/
├── data/
│   ├── plant_combined.csv   # raw sensor data (1,514 rows)
│   ├── analysis.json        # pre-computed analysis output
│   └── analysis.js          # same data as JS variable for file:// loading
├── scripts/
│   └── analysis_export.py   # data processing and model fitting script
├── index.html               # research dashboard (dark theme)
├── index2.html              # research dashboard (light theme)
└── README.md
```

## Running the dashboard

No server or build step required. Open either HTML file directly in a browser:

```
open index2.html
```

Both dashboards load `data/analysis.js` via a `<script>` tag and run entirely client-side. Chart.js 4.4 is loaded from CDN — an internet connection is required on first load (cached thereafter).

## Regenerating the analysis

Requires Python 3 with `pandas` and `numpy`.

```bash
cd Dashboard_IoT
python3 scripts/analysis_export.py
```

This reads `data/plant_combined.csv` and writes `data/analysis.json` and `data/analysis.js`. Runtime is under 2 seconds on a standard laptop.

## What analysis_export.py does

**Input:** `data/plant_combined.csv` — 1,514 rows, 30-minute intervals, columns: `ts`, `temp_c`, `hum_pct`, `soil1`, `soil2`, `soil_ref`, `light_raw`, `pump_event`.

**Derived columns:** `light_intensity = 4095 − light_raw`, `soil_rate = soil2.diff() / 0.5` (ADC/h), `vpd = 0.6108·exp(17.27T/(T+237.3))·(1−RH/100)`, `stomata = max(0, cos((hour−13)·π/12))`.

**Data cleaning:** rows within ±1 to +3 of each pump event are excluded from correlation analysis (69 rows removed, 1,445 remain).

**Drying cycle extraction:** consecutive pump event pairs are extracted as cycles. Cycles with fewer than 6 readings or a soil range below 50 ADC are discarded. The first two cycles (deployment period, Feb 13–20) are excluded from the cycle-level model due to atypical environmental conditions (avg light 610–638 ADC vs 720–1115 for cycles 1–11); all row-level data including these cycles is retained for point-to-point analyses. 11 cycles remain for model fitting.

**Analyses computed:**
- Point-to-point and cycle-level Pearson correlations (light, VPD, temperature, humidity, stomata rhythm vs soil drying rate)
- Day/night drying rate comparison (Welch t-test)
- FFT spectral analysis of the drying rate time series
- Light intensity binned analysis and cross-correlation lag
- VPD binned analysis
- Exponential drying curve fit (time constant τ per cycle)
- Sequential variance decomposition
- Stomata stress index (early vs late cycle drying rate)

**Model:**
- *Hourly model (Ridge, α=0.5):* predicts Δsoil/h from stomata×light, normalised light, VPD, temperature, cycle phase. Trained on cycles 1–8, tested on cycles 9–11.
- *Cycle model (Ridge, α=0.1):* predicts average cycle drying rate from avg light, avg VPD, avg temperature. Evaluated with Leave-One-Out cross-validation across all 11 cycles (LOO-MAE = 1.1 ADC/h, MAPE = 13.2%).

**Output:** `data/analysis.json` and `data/analysis.js` (same content, JS wraps the JSON in `window.__ANALYSIS__ = ...`).

## Sensor calibration

| Parameter | Value |
|---|---|
| Capacitive sensor wet threshold | 265 ADC |
| Capacitive sensor dry threshold | 980 ADC |
| Pump trigger (on) | ≥ 800 ADC |
| Pump trigger (off) | ≤ 280 ADC |
| Polling interval | 30 min |
