import pandas as pd
import numpy as np
import json
from datetime import datetime

df = pd.read_csv('data/plant_combined.csv', parse_dates=['ts'])
df = df.sort_values('ts').drop_duplicates('ts').reset_index(drop=True)

df['light_intensity'] = 4095 - df['light_raw']
df['soil_rate']       = df['soil2'].diff() / 0.5
df['hour']            = df['ts'].dt.hour + df['ts'].dt.minute / 60
df['date']            = df['ts'].dt.date.astype(str)

def sat_vp(T):
    return 0.6108 * np.exp(17.27 * T / (T + 237.3))

df['vpd'] = sat_vp(df['temp_c']) * (1 - df['hum_pct'] / 100)
df['stomata'] = np.maximum(0, np.cos((df['hour'] - 13) * np.pi / 12))

temp_diff = df['temp_c'].diff(2)
hum_diff  = df['hum_pct'].diff(2)
is_night  = (df['ts'].dt.hour < 7) | (df['ts'].dt.hour > 22)
df['is_heating'] = (temp_diff > 2.0) & (hum_diff < -3) & is_night

pump_indices = df.index[df['pump_event'] == 1].tolist()
exclude_set = set()
for idx in pump_indices:
    for offset in range(-1, 4):
        t = idx + offset
        if 0 <= t < len(df):
            exclude_set.add(t)

df_corr = df[~df.index.isin(exclude_set) & df['soil_rate'].notna()].copy()
print(f"rows after cleaning: {len(df_corr)}")

def pearsonr(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    n = int(mask.sum())
    if n < 3: return 0.0, 1.0
    r = float(np.corrcoef(x, y)[0, 1])
    if np.isnan(r): return 0.0, 1.0
    t_stat = r * np.sqrt((n - 2) / max(1 - r**2, 1e-10))
    p = float(2 * (1 - 0.5*(1 + np.sign(t_stat)*(1 - np.exp(-2*t_stat**2/np.pi)))))
    return round(r, 4), round(p, 6)

def welch_ttest(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    na, nb = len(a), len(b)
    va, vb = np.var(a, ddof=1), np.var(b, ddof=1)
    se = np.sqrt(va/na + vb/nb)
    t = float((np.mean(a) - np.mean(b)) / se)
    p = float(2 * (1 - 0.5*(1 + np.sign(t)*(1 - np.exp(-2*t**2/np.pi)))))
    return round(t, 2), round(p, 6), round(float(np.mean(a)), 2), round(float(np.mean(b)), 2)

pump_locs = df.index[df['pump_event'] == 1].tolist()
drying_cycles = []

for i in range(len(pump_locs) - 1):
    loc_s = pump_locs[i]
    loc_e = pump_locs[i + 1]

    cycle = df.iloc[loc_s:loc_e]

    if len(cycle) < 6:
        continue

    dur = (cycle['ts'].iloc[-1] - cycle['ts'].iloc[0]).total_seconds() / 3600
    s_min = float(cycle['soil2'].min())
    s_max = float(cycle['soil2'].max())
    if s_max - s_min < 50:
        continue

    norm_soil = ((cycle['soil2'] - s_min) / (s_max - s_min)).round(4).tolist()
    norm_time = [round(j / (len(cycle) - 1), 4) for j in range(len(cycle))]

    t_h = (cycle['ts'] - cycle['ts'].iloc[0]).dt.total_seconds().values / 3600
    y_fit = np.log(np.maximum(s_max - cycle['soil2'].values + 1, 1))
    coeffs_fit = np.polyfit(t_h, y_fit, 1)
    tau = float(-1 / coeffs_fit[0]) if coeffs_fit[0] < 0 else None

    avg_vpd   = float((sat_vp(cycle['temp_c']) * (1 - cycle['hum_pct']/100)).mean())
    avg_light = float(cycle['light_intensity'].mean())
    avg_temp  = float(cycle['temp_c'].mean())
    avg_hum   = float(cycle['hum_pct'].mean())
    rate      = (float(cycle['soil2'].iloc[-1]) - float(cycle['soil2'].iloc[0])) / dur

    n = len(cycle)
    early = cycle.iloc[:n//4]['soil2'].diff().dropna().clip(lower=0) / 0.5
    late  = cycle.iloc[3*n//4:]['soil2'].diff().dropna().clip(lower=0) / 0.5
    early_mean = float(early.mean()) if len(early) > 0 else 0.0
    late_mean  = float(late.mean())  if len(late)  > 0 else 0.0

    drying_cycles.append({
        'cycle_num':         i + 1,
        'start_ts':          cycle['ts'].iloc[0].isoformat(),
        'end_ts':            cycle['ts'].iloc[-1].isoformat(),
        'duration_h':        round(dur, 1),
        'soil_start':        round(float(cycle['soil2'].iloc[0])),
        'soil_end':          round(float(cycle['soil2'].iloc[-1])),
        'avg_temp':          round(avg_temp, 1),
        'avg_light':         round(avg_light),
        'avg_vpd':           round(avg_vpd, 3),
        'avg_hum':           round(avg_hum, 1),
        'rate_adc_h':        round(rate, 2),
        'tau_h':             round(tau, 1) if tau else None,
        'early_rate':        round(early_mean, 2),
        'late_rate':         round(late_mean, 2),
        'stress_reduction':  round((1 - late_mean / max(early_mean, 0.01)) * 100, 1),
        'normalized_time':   norm_time,
        'normalized_soil':   norm_soil,
    })

n_cycles_raw = len(drying_cycles)
drying_cycles = drying_cycles[2:]
for i, c in enumerate(drying_cycles):
    c['cycle_num'] = i + 1
cycle_df = pd.DataFrame(drying_cycles)
print(f"drying cycles: {n_cycles_raw} detected, {len(drying_cycles)} used in model")

point_correlations = {}
for col, label in [('light_intensity','Light'), ('vpd','VPD'),
                   ('temp_c','Temperature'), ('hum_pct','Humidity'), ('stomata','Stomata Rhythm')]:
    r, p = pearsonr(df_corr[col].values, df_corr['soil_rate'].values)
    point_correlations[col] = {'label': label, 'r': r, 'p': p, 'n': len(df_corr)}

between_cycle_correlations = {}
for col, label in [('avg_light','Light'), ('avg_vpd','VPD'), ('avg_temp','Temperature'), ('avg_hum','Humidity')]:
    r, p = pearsonr(cycle_df[col].values, cycle_df['rate_adc_h'].values)
    between_cycle_correlations[col] = {
        'label': label, 'r': r, 'p': p, 'n': len(cycle_df),
        'x': cycle_df[col].round(2).tolist(),
        'y': cycle_df['rate_adc_h'].round(2).tolist(),
        'cycle_nums': cycle_df['cycle_num'].tolist(),
    }

print("cycle-level r:", {k: v['r'] for k, v in between_cycle_correlations.items()})

by_hour = df_corr.groupby(df_corr['ts'].dt.hour)['soil_rate']
daily_pattern = {
    'hours': list(range(24)),
    'mean':  [round(float(by_hour.mean().get(h, 0)), 2) for h in range(24)],
    'sem':   [round(float(by_hour.sem().get(h, 0)), 2)  for h in range(24)],
    'count': [int(by_hour.count().get(h, 0))            for h in range(24)],
}
day_rates   = df_corr[df_corr['ts'].dt.hour.between(7, 20)]['soil_rate']
night_rates = df_corr[~df_corr['ts'].dt.hour.between(7, 20)]['soil_rate']
t, p, day_mean, night_mean = welch_ttest(day_rates.values, night_rates.values)
daily_pattern['ttest'] = {
    't': t, 'p': p,
    'day_mean': day_mean, 'night_mean': night_mean,
    'ratio': round(day_mean / max(abs(night_mean), 0.01), 1),
}
print(f"day/night rate: {day_mean}/{night_mean} ADC/h  ratio={daily_pattern['ttest']['ratio']}")

series = df_corr.set_index('ts')['soil_rate'].resample('30min').mean().interpolate()
signal = series.values - series.values.mean()
fft_power  = np.abs(np.fft.rfft(signal))**2
freqs_per_h = np.fft.rfftfreq(len(signal), d=0.5)
periods_h   = np.where(freqs_per_h > 0, 1.0 / freqs_per_h, 0)

peak_idx = np.argsort(fft_power[1:])[-5:] + 1
fft_peaks = [{'period_h': round(float(periods_h[i]), 1),
              'power': round(float(fft_power[i]), 1)}
             for i in sorted(peak_idx, key=lambda x: -fft_power[x])]

fft_spectrum = []
step = max(1, len(fft_power)//500)
for i in range(1, len(fft_power), step):
    if 2 < periods_h[i] < 100:
        fft_spectrum.append({'period_h': round(float(periods_h[i]), 2),
                              'power': round(float(fft_power[i]), 1)})

spectral = {'peaks': fft_peaks, 'spectrum': fft_spectrum,
            'dominant_period_h': fft_peaks[0]['period_h'] if fft_peaks else 24}
print(f"FFT dominant period: {spectral['dominant_period_h']}h")

light_bins   = [0, 100, 600, 1500, 4095]
light_labels = ['Dark (0–100)', 'Low Light (100–600)',
                'Indirect Sun (600–1500)', 'Direct Sun (1500+)']
df_corr = df_corr.copy()
df_corr['light_bin'] = pd.cut(df_corr['light_intensity'], bins=light_bins, labels=light_labels)
lg = df_corr.groupby('light_bin', observed=True)['soil_rate']
light_binned = {
    'labels': light_labels,
    'mean':   [round(float(lg.mean().get(l, 0)), 2) for l in light_labels],
    'sem':    [round(float(lg.sem().get(l, 0)), 2)  for l in light_labels],
    'count':  [int(lg.count().get(l, 0))             for l in light_labels],
    'ratio':  round(float(lg.mean().iloc[-1] / max(abs(lg.mean().iloc[0]), 0.01)), 1),
}

light_curve = []
for lo in range(0, 3900, 200):
    hi = lo + 200
    sub = df_corr[(df_corr['light_intensity'] >= lo) & (df_corr['light_intensity'] < hi)]
    if len(sub) >= 5:
        light_curve.append({'light_mid': lo + 100,
                            'mean': round(float(sub['soil_rate'].mean()), 2),
                            'sem':  round(float(sub['soil_rate'].sem()), 2),
                            'n':    len(sub)})

def cross_corr_lag(x, y, max_lag=12):
    results = []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            xi = x[lag:]
            yi = y[:len(x)-lag] if lag > 0 else y
        else:
            xi = x[:lag]
            yi = y[-lag:]
        r, _ = pearsonr(xi, yi)
        results.append({'lag_h': round(lag * 0.5, 1), 'r': r})
    return results

cross_corr = cross_corr_lag(
    df_corr['light_intensity'].fillna(0).values,
    df_corr['soil_rate'].fillna(0).values
)
peak_lag = max(cross_corr, key=lambda x: x['r'])['lag_h']

light_analysis = {
    'binned': light_binned,
    'response_curve': light_curve,
    'cross_corr': cross_corr,
    'peak_lag_h': peak_lag,
}
print(f"Light ratio dark→direct: {light_binned['ratio']}, peak lag: {peak_lag}h")

stomata_stress = {
    'avg_early_rate': round(float(cycle_df['early_rate'].mean()), 2),
    'avg_late_rate':  round(float(cycle_df['late_rate'].mean()), 2),
    'avg_reduction':  round(float(cycle_df['stress_reduction'].mean()), 1),
    'cycles': [{'cycle_num': int(r['cycle_num']),
                'early_rate': round(float(r['early_rate']), 2),
                'late_rate':  round(float(r['late_rate']), 2),
                'reduction':  round(float(r['stress_reduction']), 1)}
               for _, r in cycle_df.iterrows()],
}

vpd_bins   = [0, 0.7, 1.1, 1.6, 3.0]
vpd_labels = ['Humid (<0.7)', 'Normal (0.7–1.1)', 'Dry (1.1–1.6)', 'Very Dry (>1.6)']
df_corr['vpd_bin'] = pd.cut(df_corr['vpd'], bins=vpd_bins, labels=vpd_labels)
vg = df_corr.groupby('vpd_bin', observed=True)['soil_rate']
vpd_binned = {
    'labels': vpd_labels,
    'mean':   [round(float(vg.mean().get(l, 0)), 2) for l in vpd_labels],
    'sem':    [round(float(vg.sem().get(l, 0)), 2)  for l in vpd_labels],
    'count':  [int(vg.count().get(l, 0))             for l in vpd_labels],
    'ratio':  round(float(vg.mean().iloc[-1] / max(abs(vg.mean().iloc[0]), 0.01)), 1),
}
print(f"VPD ratio humid→very dry: {vpd_binned['ratio']}")

is_dark_night   = is_night & (df_corr['light_intensity'] < 100)
normal_night    = df_corr[is_dark_night & ~df_corr['is_heating']]['soil_rate']
heating_night   = df_corr[is_dark_night &  df_corr['is_heating']]['soil_rate']

ht, hp, h_mean, n_mean = welch_ttest(heating_night.values, normal_night.values)
heating_effect = {
    'normal_mean':  round(n_mean, 2),
    'heating_mean': round(h_mean, 2),
    'delta':        round(h_mean - n_mean, 2),
    'n_normal':     len(normal_night),
    'n_heating':    len(heating_night),
    't': ht, 'p': hp,
}
print(f"heating effect: Δ={heating_effect['delta']} ADC/h  p={hp:.3f}")

retention_curve = []
for lo in range(250, 800, 50):
    hi = lo + 50
    sub = df_corr[(df_corr['soil2'] >= lo) & (df_corr['soil2'] < hi)]
    if len(sub) >= 5:
        retention_curve.append({'soil_mid': lo + 25,
                                'mean': round(float(sub['soil_rate'].mean()), 2),
                                'sem':  round(float(sub['soil_rate'].sem()), 2),
                                'n':    len(sub)})

tau_data = cycle_df[cycle_df['tau_h'].notna()][['cycle_num','tau_h','avg_vpd','avg_light']]
r_tau_vpd, p_tau_vpd = pearsonr(tau_data['avg_vpd'].values, tau_data['tau_h'].values)

physical_drying = {
    'retention_curve': retention_curve,
    'tau_vs_vpd': {
        'r': r_tau_vpd, 'p': p_tau_vpd,
        'x': tau_data['avg_vpd'].round(3).tolist(),
        'y': tau_data['tau_h'].round(1).tolist(),
        'cycle_nums': tau_data['cycle_num'].tolist(),
        'avg_light': tau_data['avg_light'].round(0).tolist(),
    },
    'mean_tau': round(float(cycle_df['tau_h'].dropna().mean()), 1),
}
print(f"τ vs VPD r={r_tau_vpd}, mean τ={physical_drying['mean_tau']}h")

target = df_corr['soil_rate'].values
factors_ordered = [
    ('Light',          df_corr['light_intensity'].values / 4095),
    ('VPD',            df_corr['vpd'].values),
    ('Stomata Rhythm', df_corr['stomata'].values),
    ('Temperature',    df_corr['temp_c'].values),
    ('Humidity',       df_corr['hum_pct'].values),
]

X_acc = np.ones((len(target), 1))
prev_r2 = 0.0
variance_steps = []

for name, x in factors_ordered:
    X_acc = np.column_stack([X_acc, x])
    coeffs, _, _, _ = np.linalg.lstsq(X_acc, target, rcond=None)
    y_pred = X_acc @ coeffs
    ss_res = np.sum((target - y_pred)**2)
    ss_tot = np.sum((target - target.mean())**2)
    new_r2 = float(1 - ss_res / ss_tot)
    r_ind, _ = pearsonr(x, target)
    variance_steps.append({
        'factor':           name,
        'individual_r2':    round(r_ind**2, 4),
        'cumulative_r2':    round(new_r2, 4),
        'incremental_r2':   round(new_r2 - prev_r2, 4),
        'incremental_pct':  round((new_r2 - prev_r2) * 100, 1),
    })
    prev_r2 = new_r2

variance_decomposition = {
    'steps': variance_steps,
    'total_r2': round(prev_r2, 4),
    'total_pct': round(prev_r2 * 100, 1),
    'unexplained_pct': round((1 - prev_r2) * 100, 1),
}
print(f"Total R²: {variance_decomposition['total_r2']} ({variance_decomposition['total_pct']}%)")

SOIL_WET_VAL = 280
SOIL_DRY_VAL = 800

df_corr = df_corr.copy()
df_corr['cycle_phase'] = np.nan

for i in range(len(pump_locs) - 1):
    loc_s = pump_locs[i]
    loc_e = pump_locs[i + 1]
    mask = df_corr.index[(df_corr.index >= loc_s) & (df_corr.index < loc_e)]
    if len(mask) == 0:
        continue
    soil_vals = df_corr.loc[mask, 'soil2'].values
    phase = (soil_vals - SOIL_WET_VAL) / (SOIL_DRY_VAL - SOIL_WET_VAL)
    df_corr.loc[mask, 'cycle_phase'] = np.clip(phase, 0, 1)

df_corr['stomata_x_light'] = df_corr['stomata'] * (df_corr['light_intensity'] / 4095)

df_model = df_corr.dropna(subset=['cycle_phase', 'soil_rate']).copy()
print(f"Model training rows: {len(df_model)}")

def ridge_fit(X, y, alpha=1.0):
    n, p = X.shape
    A = X.T @ X
    A[1:, 1:] += alpha * np.eye(p - 1)
    return np.linalg.solve(A, X.T @ y)

def r2_score(y_true, y_pred):
    ss_res = np.sum((y_true - y_pred)**2)
    ss_tot = np.sum((y_true - y_true.mean())**2)
    return float(1 - ss_res / ss_tot)

feature_names = ['stomata_x_light', 'light_intensity', 'vpd', 'temp_c', 'cycle_phase']

X_h = np.column_stack([np.ones(len(df_model))] +
                      [df_model[f].values if f != 'light_intensity'
                       else df_model[f].values / 4095
                       for f in feature_names])
y_h = df_model['soil_rate'].values

cycle_nums_hourly = []
for i in range(len(pump_locs) - 1):
    loc_s, loc_e = pump_locs[i], pump_locs[i+1]
    mask = df_model.index[(df_model.index >= loc_s) & (df_model.index < loc_e)]
    cycle_nums_hourly.extend([i+1] * len(mask))
cycle_nums_hourly = np.array(cycle_nums_hourly[:len(df_model)])

train_mask_h = cycle_nums_hourly <= 8
test_mask_h  = cycle_nums_hourly > 8

hourly_coeffs = ridge_fit(X_h[train_mask_h], y_h[train_mask_h], alpha=0.5)
r2_train_h = r2_score(y_h[train_mask_h], X_h[train_mask_h] @ hourly_coeffs)
r2_test_h  = r2_score(y_h[test_mask_h],  X_h[test_mask_h]  @ hourly_coeffs)
r2_all_h   = r2_score(y_h, X_h @ hourly_coeffs)

print(f"hourly model R²: train={r2_train_h*100:.1f}%  test={r2_test_h*100:.1f}%  all={r2_all_h*100:.1f}%")
coeff_labels = ['intercept'] + feature_names
for name, val in zip(coeff_labels, hourly_coeffs):
    print(f"  β_{name}: {val:.4f}")

n_cycles = len(cycle_df)
X_cyc_all_feat = np.column_stack([np.ones(n_cycles),
                                   cycle_df['avg_light'].values,
                                   cycle_df['avg_vpd'].values,
                                   cycle_df['avg_temp'].values])
y_cyc = cycle_df['rate_adc_h'].values

loo_preds = np.zeros(n_cycles)
for i in range(n_cycles):
    mask = np.ones(n_cycles, dtype=bool)
    mask[i] = False
    coeffs_i = ridge_fit(X_cyc_all_feat[mask], y_cyc[mask], alpha=0.1)
    loo_preds[i] = X_cyc_all_feat[i] @ coeffs_i

errors     = loo_preds - y_cyc
errors_pct = errors / np.abs(y_cyc) * 100
mae  = float(np.mean(np.abs(errors)))
mape = float(np.mean(np.abs(errors_pct)))

print(f"cycle model LOO (n={n_cycles}): MAE={mae:.2f} ADC/h  MAPE={mape:.1f}%")
for i in range(n_cycles):
    c = cycle_df.iloc[i]
    print(f"  C{int(c['cycle_num'])}: actual={c['rate_adc_h']:.2f}  pred={loo_preds[i]:.2f}  err={errors[i]:+.2f} ({errors_pct[i]:+.1f}%)")

cycle_coeffs = ridge_fit(X_cyc_all_feat, y_cyc, alpha=0.1)
all_preds    = X_cyc_all_feat @ cycle_coeffs

validation = []
for i in range(n_cycles):
    c = cycle_df.iloc[i]
    validation.append({
        'cycle_num':  int(c['cycle_num']),
        'start_date': c['start_ts'][:10],
        'actual':     round(float(c['rate_adc_h']), 2),
        'predicted':  round(float(loo_preds[i]), 2),
        'error':      round(float(errors[i]), 2),
        'error_pct':  round(float(errors_pct[i]), 1),
        'is_test':    True,
    })

all_validation = [{'cycle_num': int(cycle_df.iloc[i]['cycle_num']),
                   'actual':    round(float(y_cyc[i]), 2),
                   'predicted': round(float(loo_preds[i]), 2),
                   'is_test':   True}
                  for i in range(n_cycles)]

n_train = n_cycles

prediction_model = {
    'cycle_model': {
        'formula': 'avg_rate = β₀ + β₁·avg_light + β₂·avg_vpd + β₃·avg_temp',
        'coefficients': {
            'intercept': round(float(cycle_coeffs[0]), 2),
            'light':     round(float(cycle_coeffs[1]), 5),
            'vpd':       round(float(cycle_coeffs[2]), 2),
            'temp':      round(float(cycle_coeffs[3]), 2),
        },
        'n_train': n_train,
        'mae':  round(mae, 2),
        'mape': round(mape, 1),
        'validation_held_out': validation,
        'validation_all': all_validation,
    },
    'hourly_model': {
        'formula': 'rate(h) = β₀ + β₁·(stomata×light) + β₂·light + β₃·VPD + β₄·temp + β₅·cycle_phase',
        'coefficients': {
            'intercept':       round(float(hourly_coeffs[0]), 2),
            'stomata_x_light': round(float(hourly_coeffs[1]), 2),
            'light':           round(float(hourly_coeffs[2]), 2),
            'vpd':             round(float(hourly_coeffs[3]), 2),
            'temp':            round(float(hourly_coeffs[4]), 2),
            'cycle_phase':     round(float(hourly_coeffs[5]), 2),
        },
        'r2':       round(r2_all_h, 4),
        'r2_train': round(r2_train_h, 4),
        'r2_test':  round(r2_test_h, 4),
    },
}

PUMP_THRESHOLD = 800
SOIL_WET = 280

scenarios = []
for name, icon, avg_light, avg_vpd, avg_temp in [
    ('Sunny March Day', '☀️', 1800, 1.6, 22),
    ('Cloudy Day',      '⛅', 400,  0.9, 18),
    ('Winter Day',      '🌧️', 150,  0.6, 15),
    ('Hot Summer Day',  '🔥', 2500, 2.0, 28),
]:
    c = hourly_coeffs
    trajectory = []
    soil = float(SOIL_WET)
    for hour in range(500):
        trajectory.append({'hour': hour, 'soil': round(soil, 1)})
        hh = hour % 24
        stomata = max(0, np.cos((hh - 13) * np.pi / 12))
        rate = max(0, c[0] + c[1]*stomata + c[2]*(avg_light/4095) + c[3]*avg_vpd + c[4]*avg_temp)
        soil += rate
        if soil >= PUMP_THRESHOLD:
            break
    days_to_pump = len(trajectory) / 24
    avg_rate = np.mean([max(0, c[0] + c[1]*max(0,np.cos((h%24-13)*np.pi/12))
                           + c[2]*(avg_light/4095) + c[3]*avg_vpd + c[4]*avg_temp)
                        for h in range(48)])
    scenarios.append({
        'name': name, 'icon': icon,
        'avg_light': avg_light, 'avg_vpd': avg_vpd, 'avg_temp': avg_temp,
        'days_to_pump': round(days_to_pump, 1),
        'avg_rate': round(float(avg_rate), 2),
        'trajectory': trajectory,
    })

for s in scenarios:
    print(f"  {s['name']}: {s['days_to_pump']} days")

analysis = {
    'meta': {
        'generated_at': datetime.now().isoformat(),
        'n_points': len(df),
        'date_range': [df['ts'].min().strftime('%Y-%m-%d'), df['ts'].max().strftime('%Y-%m-%d')],
        'n_pump_events': int(df['pump_event'].sum()),
        'n_cycles_raw': n_cycles_raw,
        'n_cycles': len(drying_cycles),
        'avg_temp': round(float(df['temp_c'].mean()), 1),
        'avg_humidity': round(float(df['hum_pct'].mean()), 1),
        'avg_cycle_duration_h': round(float(cycle_df['duration_h'].mean()), 1),
        'location': 'London, UK',
        'calibration': {'soil_wet': 265, 'soil_dry': 980, 'pump_threshold': 800},
    },
    'timeseries': [
        {'ts': r['ts'].isoformat(),
         'soil2': int(r['soil2']),
         'temp_c': round(float(r['temp_c']), 1),
         'hum_pct': round(float(r['hum_pct']), 1),
         'light_intensity': int(r['light_intensity']),
         'vpd': round(float(r['vpd']), 3),
         'pump_event': int(r['pump_event']),
         'soil_rate': round(float(r['soil_rate']), 2) if pd.notna(r['soil_rate']) else None}
        for _, r in df.iterrows()
    ],
    'heatmap': [
        {'date': str(date), 'hour': int(hour),
         'soil_rate': round(float(grp['soil_rate'].mean()), 2)}
        for (date, hour), grp in df_corr.groupby(
            [df_corr['ts'].dt.date.astype(str), df_corr['ts'].dt.hour])
        if len(grp) > 0
    ],
    'drying_cycles': drying_cycles,
    'point_correlations': point_correlations,
    'between_cycle_correlations': between_cycle_correlations,
    'daily_pattern': daily_pattern,
    'spectral': spectral,
    'light_analysis': light_analysis,
    'stomata_stress': stomata_stress,
    'vpd_binned': vpd_binned,
    'heating_effect': heating_effect,
    'physical_drying': physical_drying,
    'variance_decomposition': variance_decomposition,
    'prediction_model': prediction_model,
    'scenarios': scenarios,
}

with open('data/analysis.json', 'w') as f:
    json.dump(analysis, f, indent=2, default=str)

raw_json = json.dumps(analysis, default=str)
with open('data/analysis.js', 'w') as f:
    f.write('window.__ANALYSIS__ = ')
    f.write(raw_json)
    f.write(';')

print(f"\nDone. {len(df)} rows, {n_cycles_raw} cycles ({len(drying_cycles)} in model) → data/analysis.json + data/analysis.js")
