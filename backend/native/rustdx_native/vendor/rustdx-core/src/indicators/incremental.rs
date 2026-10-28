//! Constant-memory EMA/MACD state. Updates consume ordered observations.
//! A missing/nonfinite observation resets its smoothing segment; no timestamp,
//! frequency, closed-bar or price-basis qualification is inferred here.
use super::MacdResult;

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum IndicatorError {
    #[error("indicator periods must be positive")]
    ZeroPeriod,
    #[error("histogram scale must be finite and positive")]
    InvalidHistogramScale,
}

/// EMA seeded with the first finite observation, alpha = 2/(period+1).
/// Clone to checkpoint a provisional bar; replay corrections from a checkpoint.
#[derive(Debug, Clone)]
pub struct EmaState {
    period: usize,
    alpha: f64,
    previous: Option<f64>,
}

impl EmaState {
    pub fn new(period: usize) -> Result<Self, IndicatorError> {
        if period == 0 {
            return Err(IndicatorError::ZeroPeriod);
        }
        Ok(Self {
            period,
            alpha: 2.0 / (period as f64 + 1.0),
            previous: None,
        })
    }

    pub fn period(&self) -> usize {
        self.period
    }

    pub fn reset(&mut self) {
        self.previous = None;
    }

    /// None, NaN, infinity or arithmetic overflow emits None and resets EMA.
    pub fn update(&mut self, observation: Option<f64>) -> Option<f64> {
        self.previous = observation.filter(|v| v.is_finite()).and_then(|price| {
            let value = self
                .previous
                .map_or(price, |prev| price * self.alpha + prev * (1.0 - self.alpha));
            value.is_finite().then_some(value)
        });
        self.previous
    }
}

/// Both period orders are supported, including equal periods (zero difference).
/// `histogram_scale=1` is the legacy contract; use 2 explicitly for double bars.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MacdParameters {
    pub fast_period: usize,
    pub slow_period: usize,
    pub signal_period: usize,
    pub histogram_scale: f64,
}

impl Default for MacdParameters {
    fn default() -> Self {
        Self {
            fast_period: 12,
            slow_period: 26,
            signal_period: 9,
            histogram_scale: 1.0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MacdPoint {
    pub macd: Option<f64>,
    pub signal: Option<f64>,
    pub histogram: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct MacdState {
    parameters: MacdParameters,
    fast: EmaState,
    slow: EmaState,
    signal: EmaState,
}

impl MacdState {
    pub fn new(parameters: MacdParameters) -> Result<Self, IndicatorError> {
        if !parameters.histogram_scale.is_finite() || parameters.histogram_scale <= 0.0 {
            return Err(IndicatorError::InvalidHistogramScale);
        }
        Ok(Self {
            parameters,
            fast: EmaState::new(parameters.fast_period)?,
            slow: EmaState::new(parameters.slow_period)?,
            signal: EmaState::new(parameters.signal_period)?,
        })
    }

    pub fn parameters(&self) -> MacdParameters {
        self.parameters
    }

    pub fn reset(&mut self) {
        self.fast.reset();
        self.slow.reset();
        self.signal.reset();
    }

    /// A gap resets all EMAs; a nonfinite fast-minus-slow resets the signal EMA.
    /// Histogram overflow emits None without changing the other two lines.
    pub fn update(&mut self, observation: Option<f64>) -> MacdPoint {
        let macd = self
            .fast
            .update(observation)
            .zip(self.slow.update(observation))
            .and_then(|(fast, slow)| (fast - slow).is_finite().then_some(fast - slow));
        let signal = self.signal.update(macd);
        let histogram = macd.zip(signal).and_then(|(macd, signal)| {
            let value = (macd - signal) * self.parameters.histogram_scale;
            value.is_finite().then_some(value)
        });
        MacdPoint {
            macd,
            signal,
            histogram,
        }
    }
}

/// Checked EMA; invalid parameters return Err even for empty input.
pub fn try_ema(data: &[f64], period: usize) -> Result<Vec<Option<f64>>, IndicatorError> {
    let mut state = EmaState::new(period)?;
    Ok(data.iter().map(|&v| state.update(Some(v))).collect())
}

/// Checked batch MACD uses exactly the same recurrence as streaming updates.
pub fn try_macd(data: &[f64], parameters: MacdParameters) -> Result<MacdResult, IndicatorError> {
    let mut state = MacdState::new(parameters)?;
    let mut result = MacdResult {
        macd: Vec::with_capacity(data.len()),
        signal: Vec::with_capacity(data.len()),
        histogram: Vec::with_capacity(data.len()),
    };
    for &value in data {
        let point = state.update(Some(value));
        result.macd.push(point.macd);
        result.signal.push(point.signal);
        result.histogram.push(point.histogram);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn published_recurrence_has_known_values_and_explicit_histogram_scale() {
        let mut state = MacdState::new(MacdParameters {
            fast_period: 2,
            slow_period: 3,
            signal_period: 2,
            histogram_scale: 2.0,
        })
        .unwrap();
        let initial = state.update(Some(10.0));
        assert_eq!(initial.macd, Some(0.0));
        assert_eq!(initial.signal, Some(0.0));
        let next = state.update(Some(12.0));
        assert!((next.macd.unwrap() - 1.0 / 3.0).abs() < 1e-14);
        assert!((next.signal.unwrap() - 2.0 / 9.0).abs() < 1e-14);
        assert!((next.histogram.unwrap() - 2.0 / 9.0).abs() < 1e-14);
        let third = state.update(Some(14.0));
        assert!((third.macd.unwrap() - 11.0 / 18.0).abs() < 1e-14);
        assert!((third.signal.unwrap() - 13.0 / 27.0).abs() < 1e-14);
    }

    #[test]
    fn chunks_gaps_and_checkpoint_replay_preserve_the_same_state() {
        let data = [10.0, 12.0, f64::NAN, 15.0, 18.0, f64::INFINITY, 21.0];
        let parameters = MacdParameters::default();
        let expected = try_macd(&data, parameters).unwrap();
        for split in 0..=data.len() {
            let mut state = MacdState::new(parameters).unwrap();
            let points: Vec<_> = data[..split]
                .iter()
                .chain(&data[split..])
                .map(|&v| state.update(Some(v)))
                .collect();
            assert_eq!(
                points.iter().map(|p| p.macd).collect::<Vec<_>>(),
                expected.macd
            );
            assert_eq!(
                points.iter().map(|p| p.signal).collect::<Vec<_>>(),
                expected.signal
            );
            assert_eq!(
                points.iter().map(|p| p.histogram).collect::<Vec<_>>(),
                expected.histogram
            );
        }
        let mut state = EmaState::new(3).unwrap();
        assert_eq!(state.update(Some(10.0)), Some(10.0));
        let checkpoint = state.clone();
        assert_eq!(state.update(Some(12.0)), Some(11.0));
        state = checkpoint;
        assert_eq!(state.update(Some(14.0)), Some(12.0));
        assert_eq!(state.update(None), None);
        assert_eq!(state.update(Some(20.0)), Some(20.0));
        state.reset();
        assert_eq!(state.update(Some(42.0)), Some(42.0));
    }

    #[test]
    fn checked_parameters_reject_empty_input_and_accept_large_periods() {
        assert!(try_ema(&[], 0).is_err());
        assert!(
            try_macd(
                &[],
                MacdParameters {
                    fast_period: 0,
                    ..Default::default()
                }
            )
            .is_err()
        );
        for scale in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert!(
                try_macd(
                    &[],
                    MacdParameters {
                        histogram_scale: scale,
                        ..Default::default()
                    }
                )
                .is_err()
            );
        }
        assert_eq!(try_ema(&[42.0], usize::MAX).unwrap(), [Some(42.0)]);
    }
}
