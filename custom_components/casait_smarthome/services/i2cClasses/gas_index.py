"""Sensirion gas index algorithm, VOC variant, in pure Python.

The SGP40 reports a raw resistance signal. Turning it into the 1-500 VOC index
takes Sensirion's adaptive algorithm, which learns the sensor's baseline over
hours. The official Python package wraps the C implementation and ships no
wheel Home Assistant could install, so this is a line-by-line port of
``sensirion_gas_index_algorithm.c`` v3.2.0, restricted to the VOC path.

Copyright (c) 2022, Sensirion AG. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
* Neither the name of Sensirion AG nor the names of its contributors may be used
  to endorse or promote products derived from this software without specific
  prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

import math

INITIAL_BLACKOUT = 45.0
INDEX_GAIN = 230.0
SRAW_STD_INITIAL = 50.0
SRAW_STD_BONUS_VOC = 220.0
TAU_MEAN_HOURS = 12.0
TAU_VARIANCE_HOURS = 12.0
TAU_INITIAL_MEAN_VOC = 20.0
INIT_DURATION_MEAN_VOC = 3600.0 * 0.75
INIT_TRANSITION_MEAN = 0.01
TAU_INITIAL_VARIANCE = 2500.0
INIT_DURATION_VARIANCE_VOC = 3600.0 * 1.45
INIT_TRANSITION_VARIANCE = 0.01
GATING_THRESHOLD_VOC = 340.0
GATING_THRESHOLD_INITIAL = 510.0
GATING_THRESHOLD_TRANSITION = 0.09
GATING_VOC_MAX_DURATION_MINUTES = 60.0 * 3.0
GATING_MAX_RATIO = 0.3
SIGMOID_L = 500.0
SIGMOID_K_VOC = -0.0065
SIGMOID_X0_VOC = 213.0
VOC_INDEX_OFFSET_DEFAULT = 100.0
LP_TAU_FAST = 20.0
LP_TAU_SLOW = 500.0
LP_ALPHA = -0.2
VOC_SRAW_MINIMUM = 20000
PERSISTENCE_UPTIME_GAMMA = 3.0 * 3600.0
MVE_GAMMA_SCALING = 64.0
MVE_ADDITIONAL_GAMMA_MEAN_SCALING = 8.0
MVE_FIX16_MAX = 32767.0


def _sigmoid(k: float, x0: float, sample: float) -> float:
    x = k * (sample - x0)
    if x < -50.0:
        return 1.0
    if x > 50.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(x))


class VocGasIndexAlgorithm:
    """Stateful VOC index calculation for one SGP40.

    ``process`` has to be fed at the sampling interval the instance was built
    with; the learning time constants are expressed in samples.
    """

    def __init__(self, sampling_interval: float = 1.0) -> None:
        """Initialize the algorithm for the given sampling interval in seconds."""

        self.sampling_interval = sampling_interval
        self._index_offset = VOC_INDEX_OFFSET_DEFAULT
        self._sraw_minimum = VOC_SRAW_MINIMUM
        self._gating_max_duration_minutes = GATING_VOC_MAX_DURATION_MINUTES
        self._init_duration_mean = INIT_DURATION_MEAN_VOC
        self._init_duration_variance = INIT_DURATION_VARIANCE_VOC
        self._gating_threshold = GATING_THRESHOLD_VOC
        self._index_gain = INDEX_GAIN
        self._tau_mean_hours = TAU_MEAN_HOURS
        self._tau_variance_hours = TAU_VARIANCE_HOURS
        self._sraw_std_initial = SRAW_STD_INITIAL
        self.reset()

    def reset(self) -> None:
        """Forget everything learned so far."""

        self._uptime = 0.0
        self._sraw = 0.0
        self._gas_index = 0.0
        self._init_instances()

    def _init_instances(self) -> None:
        interval = self.sampling_interval
        # Mean/variance estimator
        self._mve_initialized = False
        self._mve_mean = 0.0
        self._mve_sraw_offset = 0.0
        self._mve_std = self._sraw_std_initial
        self._mve_gamma_mean_base = (MVE_ADDITIONAL_GAMMA_MEAN_SCALING * MVE_GAMMA_SCALING * (interval / 3600.0)) / (
            self._tau_mean_hours + interval / 3600.0
        )
        self._mve_gamma_variance_base = (MVE_GAMMA_SCALING * (interval / 3600.0)) / (
            self._tau_variance_hours + interval / 3600.0
        )
        self._mve_gamma_initial_mean = (MVE_ADDITIONAL_GAMMA_MEAN_SCALING * MVE_GAMMA_SCALING * interval) / (
            TAU_INITIAL_MEAN_VOC + interval
        )
        self._mve_gamma_initial_variance = (MVE_GAMMA_SCALING * interval) / (TAU_INITIAL_VARIANCE + interval)
        self._mve_gamma_mean = 0.0
        self._mve_gamma_variance = 0.0
        self._mve_uptime_gamma = 0.0
        self._mve_uptime_gating = 0.0
        self._mve_gating_duration_minutes = 0.0
        # MOX model
        self._mox_std = self._mve_std
        self._mox_mean = self._mve_mean + self._mve_sraw_offset
        # Adaptive low pass
        self._lp_a1 = interval / (LP_TAU_FAST + interval)
        self._lp_a2 = interval / (LP_TAU_SLOW + interval)
        self._lp_initialized = False
        self._lp_x1 = self._lp_x2 = self._lp_x3 = 0.0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @property
    def has_baseline(self) -> bool:
        """Return whether there is a learned or restored baseline worth keeping."""

        return self._mve_initialized

    def get_states(self) -> tuple[float, float]:
        """Return the learned (mean, std) pair for storing across restarts."""

        return self._mve_mean + self._mve_sraw_offset, self._mve_std

    def set_states(self, mean: float, std: float) -> None:
        """Restore a learned (mean, std) pair.

        Sensirion recommends this only after an interruption of less than ten
        minutes; after longer the baseline may have moved.
        """

        self._mve_mean = mean
        self._mve_std = std
        self._mve_uptime_gamma = PERSISTENCE_UPTIME_GAMMA
        self._mve_initialized = True
        self._mox_std = self._mve_std
        self._mox_mean = self._mve_mean + self._mve_sraw_offset
        self._sraw = mean

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def process(self, sraw: int) -> int:
        """Feed one raw sample and return the VOC index (0 during warm-up)."""

        if self._uptime <= INITIAL_BLACKOUT:
            self._uptime += self.sampling_interval
        else:
            if 0 < sraw < 65000:
                sraw = min(max(sraw, self._sraw_minimum + 1), self._sraw_minimum + 32767)
                self._sraw = float(sraw - self._sraw_minimum)
            self._gas_index = self._mox_process(self._sraw)
            self._gas_index = self._sigmoid_scaled_process(self._gas_index)
            self._gas_index = self._lowpass_process(self._gas_index)
            self._gas_index = max(self._gas_index, 0.5)
            if self._sraw > 0.0:
                self._mve_process(self._sraw)
                self._mox_std = self._mve_std
                self._mox_mean = self._mve_mean + self._mve_sraw_offset
        return int(self._gas_index + 0.5)

    def _mve_calculate_gamma(self) -> None:
        interval = self.sampling_interval
        uptime_limit = MVE_FIX16_MAX - interval
        if self._mve_uptime_gamma < uptime_limit:
            self._mve_uptime_gamma += interval
        if self._mve_uptime_gating < uptime_limit:
            self._mve_uptime_gating += interval

        sigmoid_gamma_mean = _sigmoid(INIT_TRANSITION_MEAN, self._init_duration_mean, self._mve_uptime_gamma)
        gamma_mean = self._mve_gamma_mean_base + (
            (self._mve_gamma_initial_mean - self._mve_gamma_mean_base) * sigmoid_gamma_mean
        )
        gating_threshold_mean = self._gating_threshold + (
            (GATING_THRESHOLD_INITIAL - self._gating_threshold)
            * _sigmoid(INIT_TRANSITION_MEAN, self._init_duration_mean, self._mve_uptime_gating)
        )
        sigmoid_gating_mean = _sigmoid(GATING_THRESHOLD_TRANSITION, gating_threshold_mean, self._gas_index)
        self._mve_gamma_mean = sigmoid_gating_mean * gamma_mean

        sigmoid_gamma_variance = _sigmoid(
            INIT_TRANSITION_VARIANCE, self._init_duration_variance, self._mve_uptime_gamma
        )
        gamma_variance = self._mve_gamma_variance_base + (
            (self._mve_gamma_initial_variance - self._mve_gamma_variance_base)
            * (sigmoid_gamma_variance - sigmoid_gamma_mean)
        )
        gating_threshold_variance = self._gating_threshold + (
            (GATING_THRESHOLD_INITIAL - self._gating_threshold)
            * _sigmoid(INIT_TRANSITION_VARIANCE, self._init_duration_variance, self._mve_uptime_gating)
        )
        sigmoid_gating_variance = _sigmoid(GATING_THRESHOLD_TRANSITION, gating_threshold_variance, self._gas_index)
        self._mve_gamma_variance = sigmoid_gating_variance * gamma_variance

        self._mve_gating_duration_minutes += (interval / 60.0) * (
            ((1.0 - sigmoid_gating_mean) * (1.0 + GATING_MAX_RATIO)) - GATING_MAX_RATIO
        )
        self._mve_gating_duration_minutes = max(self._mve_gating_duration_minutes, 0.0)
        if self._mve_gating_duration_minutes > self._gating_max_duration_minutes:
            self._mve_uptime_gating = 0.0

    def _mve_process(self, sraw: float) -> None:
        if not self._mve_initialized:
            self._mve_initialized = True
            self._mve_sraw_offset = sraw
            self._mve_mean = 0.0
            return

        if self._mve_mean >= 100.0 or self._mve_mean <= -100.0:
            self._mve_sraw_offset += self._mve_mean
            self._mve_mean = 0.0
        sraw -= self._mve_sraw_offset
        self._mve_calculate_gamma()
        delta_sgp = (sraw - self._mve_mean) / MVE_GAMMA_SCALING
        c = self._mve_std - delta_sgp if delta_sgp < 0.0 else self._mve_std + delta_sgp
        additional_scaling = (c / 1440.0) * (c / 1440.0) if c > 1440.0 else 1.0
        self._mve_std = math.sqrt(additional_scaling * (MVE_GAMMA_SCALING - self._mve_gamma_variance)) * math.sqrt(
            (self._mve_std * (self._mve_std / (MVE_GAMMA_SCALING * additional_scaling)))
            + (((self._mve_gamma_variance * delta_sgp) / additional_scaling) * delta_sgp)
        )
        self._mve_mean += (self._mve_gamma_mean * delta_sgp) / MVE_ADDITIONAL_GAMMA_MEAN_SCALING

    def _mox_process(self, sraw: float) -> float:
        return ((sraw - self._mox_mean) / (-1.0 * (self._mox_std + SRAW_STD_BONUS_VOC))) * self._index_gain

    def _sigmoid_scaled_process(self, sample: float) -> float:
        x = SIGMOID_K_VOC * (sample - SIGMOID_X0_VOC)
        if x < -50.0:
            return SIGMOID_L
        if x > 50.0:
            return 0.0
        if sample >= 0.0:
            shift = (SIGMOID_L - (5.0 * self._index_offset)) / 4.0
            return ((SIGMOID_L + shift) / (1.0 + math.exp(x))) - shift
        return (self._index_offset / VOC_INDEX_OFFSET_DEFAULT) * (SIGMOID_L / (1.0 + math.exp(x)))

    def _lowpass_process(self, sample: float) -> float:
        if not self._lp_initialized:
            self._lp_x1 = self._lp_x2 = self._lp_x3 = sample
            self._lp_initialized = True
        self._lp_x1 = ((1.0 - self._lp_a1) * self._lp_x1) + (self._lp_a1 * sample)
        self._lp_x2 = ((1.0 - self._lp_a2) * self._lp_x2) + (self._lp_a2 * sample)
        abs_delta = abs(self._lp_x1 - self._lp_x2)
        f1 = math.exp(LP_ALPHA * abs_delta)
        tau_a = ((LP_TAU_SLOW - LP_TAU_FAST) * f1) + LP_TAU_FAST
        a3 = self.sampling_interval / (self.sampling_interval + tau_a)
        self._lp_x3 = ((1.0 - a3) * self._lp_x3) + (a3 * sample)
        return self._lp_x3
