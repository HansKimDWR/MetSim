""" Disaggregates daily data down to finer grained data using some heuristics
"""
# Meteorology Simulator
# Copyright (C) 2017  The Computational Hydrology Group, Department of Civil
# and Environmental Engineering, University of Washington.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np
import pandas as pd
import itertools
import scipy.interpolate

import metsim.constants as cnst
from metsim.physics import svp
from metsim.datetime import date_range


def disaggregate(df_daily: pd.DataFrame, params: dict,
                 solar_geom: dict, t_begin: list=None,
                 t_end: list=None, **kwargs):
    """
    Take a daily timeseries and scale it down to a finer
    time scale.

    Parameters
    ----------
    df_daily: pd.DataFrame
        Dataframe containing daily timeseries.
        Should be the result of one of the methods
        provided in the `methods` directory.
    params: dict
        A dictionary containing the class parameters
        of the MetSim object.
    solar_geom: dict
        A dictionary of solar geometry variables
    t_begin: list
        List of t_min and t_max for day previous to the
        start of `df_daily`. None indicates no extension
        of the record.
    t_end: list
        List of t_min and t_max for day after the end
        of `df_daily`. None indicates no extension of
        the record.

    Dynamic optional variables (kwargs)
    -----------------
    dur: pd.DataFrame
        Daily timeseries of storm durations for given month of year. [minutes]
    t_pk: pd.DataFrame
        Daily timeseries of time to storm peaks for given month of year. [minutes]

    Returns
    -------
    df_disagg:
        A dataframe with sub-daily timeseries.
    """
    stop = (df_daily.index[-1] + pd.Timedelta('1 days')
            - pd.Timedelta("{} minutes".format(params['time_step'])))
    dates_disagg = date_range(df_daily.index[0], stop,
                              freq='{}T'.format(params['time_step']))
    df_disagg = pd.DataFrame(index=dates_disagg)
    n_days = len(df_daily)
    n_disagg = len(df_disagg)
    ts = float(params['time_step'])
    df_disagg['shortwave'] = shortwave(df_daily['shortwave'],
                                       df_daily['dayl'],
                                       df_daily.index.dayofyear,
                                       solar_geom['tiny_rad_fract'],
                                       params)

    t_Tmin, t_Tmax = set_min_max_hour(df_disagg['shortwave'],
                                      n_days, ts, params)

    df_disagg['temp'] = temp(df_daily, df_disagg, t_Tmin, t_Tmax, ts,
                             t_begin, t_end)

    df_disagg['vapor_pressure'] = vapor_pressure(df_daily['vapor_pressure'],
                                                 df_disagg['temp'],
                                                 t_Tmin, n_disagg, ts)

    df_disagg['rel_humid'] = relative_humidity(df_disagg['vapor_pressure'],
                                               df_disagg['temp'])

    df_disagg['longwave'], df_disagg['tskc'] = longwave(
        df_disagg['temp'], df_disagg['vapor_pressure'],
        df_daily['tskc'], params)

    if params['prec_type'].upper() == 'TRIANGLE':
        dur = kwargs['dur']
        t_pk = kwargs['t_pk']
        df_disagg['prec'] = prec(df_daily['prec'], ts, params,
                                 dur=dur, t_pk=t_pk,
                                 day_of_year=df_daily.index.dayofyear,
                                 month_of_year=df_daily.index.month)
    else:
        df_disagg['prec'] = prec(df_daily['prec'], ts, params)

    if 'wind' in df_daily:
        df_disagg['wind'] = wind(df_daily['wind'], ts)

    return df_disagg.fillna(method='ffill')


def set_min_max_hour(disagg_rad: pd.Series, n_days: int,
                     ts: float, params: dict):
    """
    Determine the time at which min and max temp
    is reached for each day.

    Parameters
    ----------
    disagg_rad:
        Shortwave radiation disaggregated
        to sub-daily timesteps.
    n_days:
        The number of days being disaggregated
    ts:
        Timestep used for disaggregation
    params:
        A dictionary of class parameters of
        the MetSim object.

    Returns
    -------
    (t_t_min, t_t_max):
        A tuple containing 2 timeseries, corresponding
        to time of min and max temp, respectively
    """
    rad_mask = 1*(disagg_rad > 0)
    diff_mask = np.diff(rad_mask)
    rise_times = np.where(diff_mask > 0)[0] * ts
    set_times = np.where(diff_mask < 0)[0] * ts
    t_t_max = (params['tmax_daylength_fraction'] * (set_times - rise_times) +
               rise_times)
    t_t_min = rise_times
    return t_t_min, t_t_max


def temp(df_daily: pd.DataFrame, df_disagg: pd.DataFrame,
         t_t_min: np.array, t_t_max: np.array, ts: float,
         t_begin: list=None, t_end: list=None):
    """
    Disaggregate temperature using a Hermite polynomial
    interpolation scheme.

    Parameters
    ----------
    df_daily:
        A dataframe of daily values.
    df_disagg:
        A dataframe of sub-daily values.
    t_t_min:
        Times at which minimum daily
        temperatures are reached.
    t_t_max:
        Times at which maximum daily
        temperatures are reached.
    ts:
        Timestep for disaggregation
    t_begin: list
        List of t_min and t_max for day previous to the
        start of `df_daily`. None indicates no extension
        of the record.
    t_end: list
        List of t_min and t_max for day after the end
        of `df_daily`. None indicates no extension of
        the record.

    Returns
    -------
    temps:
        A sub-daily timeseries of temperature.
    """
    # Calculate times of min/max temps
    time = np.array(list(next(it) for it in itertools.cycle(
                [iter(t_t_min), iter(t_t_max)])))
    temp = np.array(list(next(it) for it in itertools.cycle(
                [iter(df_daily['t_min']), iter(df_daily['t_max'])])))
    # Account for end points
    ts_ends = cnst.MIN_PER_HOUR * cnst.HOURS_PER_DAY
    time = np.append(np.insert(time, 0, time[0:2]-ts_ends), time[-2:]+ts_ends)

    # If no start or end data is provided to extend the record repeat values
    # This provides space at the ends so that extrapolation doesn't continue
    # in strange ways at the end points
    if t_begin is None:
        t_begin = temp[0:2]
    if t_end is None:
        t_end = temp[-2:]
    temp = np.append(np.insert(temp, 0, t_begin), t_end)

    # Interpolate the values
    interp = scipy.interpolate.PchipInterpolator(time, temp, extrapolate=True)
    temps = interp(ts * np.arange(0, len(df_disagg.index)))
    return temps

def prec(prec: pd.Series, ts: float, params: dict, **kwargs):
    """
    Distributes sub-daily precipitation either evenly (uniform) or with a
    triangular (triangle) distribution, depending upon the chosen method.

    Note: The uniform disaggregation returns only through to the beginning of the
          last day. Final values are filled in using a
          forward fill in the top level disaggregate function

    Parameters
    ----------
    prec:
        Daily timeseries of precipitation. [mm]
    ts:
        Timestep length to disaggregate down to. [minutes]
    params:
        A dictionary of parameters, which contains
        information about which precipitation disaggregation
        method to use.

    Dynamic optional variables:
    -------------------
    dur:
        Timeseries of climatological average monthly storm durations. [minutes]
    t_pk:
        Timeseries of climatological average monthly storm peak time. [minutes]
    day_of_year:
        Timeseries of index of days since Jan-1.
    month_of_year:
        Timeseries of month of year.

    Returns
    -------
    prec:
        A sub-daily timeseries of precipitation. [mm]
    """

    def prec_UNIFORM(prec: pd.Series, ts: float):

        scale = int(ts) / (cnst.MIN_PER_HOUR * cnst.HOURS_PER_DAY)
        P_return = (prec * scale).resample('{:0.0f}T'.format(ts)).fillna(method='ffill')
        return P_return

    def prec_TRIANGLE(prec: pd.Series, ts: float, **kwargs):

        dur = kwargs['dur']
        t_pk = kwargs['t_pk']
        day_of_year = kwargs['day_of_year']
        month_of_year = kwargs['month_of_year']
        n_days = len(prec)
        steps_per_day = int(cnst.MIN_PER_HOUR * cnst.HOURS_PER_DAY / int(ts))
        offset = np.ceil(steps_per_day / 2)
        output_length = int(steps_per_day * n_days)
        index = np.arange(output_length)
        steps_per_two_days = int(((np.ceil(steps_per_day / 2)) * 2) + steps_per_day)
        P_return = pd.Series(np.zeros(output_length, dtype='float'), index=index)

        # create kernel of unit hyetographs, one for each month
        kernels = np.zeros(shape = (12, steps_per_two_days))

        for month in np.arange(12, dtype=int):
            I_pk = 2. * 1. / dur[month]
            m = I_pk / (dur[month] / 2.)
            t_start = (t_pk[month] - (dur[month] / 2.))
            t_end = (t_pk[month] + (dur[month] / 2.))
            i_start = np.floor(t_start / ts ) + offset
            i_end = np.floor(t_end / ts ) + offset
            i_t_pk = np.floor(t_pk[month] / ts) + offset

            if (i_start == i_t_pk) and (i_end != i_start):
                for step in np.arange(steps_per_two_days, dtype = int):
                    t_step_start = (step - offset) * ts
                    t_step_end = t_step_start + ts
                    if step == i_start:
                        h1 = t_pk[month] - t_start
                        b1 = m * h1
                        A1 = b1 / 2 * h1
                        b2 = -m * (t_step_end - t_end)
                        a2 = b2 + (-m * (t_pk[month] - t_step_end))
                        A2 = (a2 + b2) / 2 * (t_step_end - t_pk[month])
                        kernels[month, step] = A1 + A2
                    elif (step > i_start) and (step < i_end):
                        b = -m * (t_step_end - t_end)
                        a = -m * (t_step_start - t_step_end)
                        kernels[month, step] = (a + b) / 2 * ts
                    elif step == i_end:
                        h = t_step_start - t_end
                        b = -m * h
                        kernels[month, step] = b / 2 * -h
                    else:
                        kernels[month, step] = 0
            elif i_t_pk == i_end and (i_end != i_start):
                for step in np.arange(steps_per_two_days, dtype = int):
                    t_step_start = (step - offset) * ts
                    t_step_end = t_step_start + ts
                    if step == i_start:
                        h = t_step_end - t_start
                        b = m * h
                        kernels[month, step] = b / 2 * h
                    elif step == i_t_pk:
                        h2 = t_pk[month] - t_end
                        b2 = -m * h2
                        A2 = b2 / 2 * -h2
                        b1 = m * (t_step_start - t_start)
                        a1 = b1 + ( m * (t_pk[month] - t_step_start))
                        A1 = (a1 + b1) / 2 * (t_pk[month] - t_step_start)
                        kernels[month, step] = A1 + A2
                    else:
                        kernels[month, step] = 0
            elif i_start == i_end:
                for step in np.arange(steps_per_two_days, dtype = int):
                    t_step_start = (step - offset) * ts
                    t_step_end = t_step_start + ts
                    if step == i_start:
                        kernels[month, step] = 1
                    else:
                        kernels[month, step] = 0
            else:
                for step in np.arange(steps_per_two_days, dtype = int):
                    t_step_start = (step - offset) * ts
                    t_step_end = t_step_start + ts
                    if step == i_start:
                        h = t_step_end - t_start
                        b = m * h
                        kernels[month, step] = b / 2 * h
                    elif (step > i_start) and (step < i_t_pk):
                        a = m * (t_step_start - t_start)
                        b = a + (m * ts)
                        kernels[month, step] = (a + b) / 2 * ts
                    elif i_t_pk == step:
                        h1 = t_pk[month] - t_step_start
                        a1 = m * (t_step_start - t_start)
                        b1 = a1 + (m * h1)
                        h2 = (t_step_end - t_pk[month])
                        b2 = -m * (t_step_end - t_end)
                        a2 = b2 + (m * h2)
                        kernels[month, step] = ((a1 + b1) / 2 * h1 ) + ((a2 + b2) / 2 * h2)
                    elif (step < i_end) and (step > i_t_pk):
                        if (t_step_end != t_end):
                            b = -m * (t_step_end - t_end)
                        else:
                            b = 0
                        a = b + ( m * ts)
                        kernels[month, step] = (a + b) / 2 * ts
                    elif (step == i_end):
                        h = t_step_start - t_end
                        if (t_step_start != t_end):
                            b = -m * h
                        else:
                            b = 0
                        kernels[month, step] = b / 2 * -h
                    else:
                        P_return[step] = 0


        # Loop through each day of the timeseries and apply the kernel for the appropriate month of year
        for d in np.arange(n_days):
            if d == 0:
                i1 = int(np.ceil(steps_per_day * 1.5))
                P_return[:i1] += (1 / sum(kernels[month_of_year[d] - 1, int(np.ceil(steps_per_day / 2)):])) * prec[d] * kernels[month_of_year[d] - 1, int(np.ceil(steps_per_day / 2)):]
            elif d == (n_days - 1):
                i0 = int(np.floor((d - 0.5) * steps_per_day))
                P_return[i0:] += (1 / sum(kernels[month_of_year[d] - 1, :int(np.ceil(steps_per_day * 1.5))])) * prec[d] * kernels[month_of_year[d] - 1, :int(np.ceil(steps_per_day * 1.5))]
            else:
                i0 = int(np.floor((d - 0.5) * steps_per_day))
                i1 = int(i0 + (2 * steps_per_day))
                P_return[i0:i1] += prec[d]*kernels[month_of_year[d] - 1, :]
        P_return = np.around(P_return,decimals=5)
        return P_return.values

    prec_function = {
        'UNIFORM': prec_UNIFORM,
        'TRIANGLE': prec_TRIANGLE,
    }

    P_return = prec_function[params['prec_type'].upper()](prec, ts, **kwargs)
    return P_return


def wind(wind: pd.Series, ts: float):
    """
    Wind is assumed constant throughout the day

    Parameters
    ----------
    wind:
        Daily timeseries of wind
    ts:
        Timestep to disaggregate down to

    Returns
    -------
    wind:
        A sub-daily timeseries of wind
    """
    return wind.resample('{:0.0f}T'.format(ts)).fillna(method='ffill')


def relative_humidity(vapor_pressure: pd.Series, temp: pd.Series):
    """
    Calculate relative humidity from vapor pressure
    and temperature.

    Parameters
    ----------
    vapor_pressure:
        A sub-daily timeseries of vapor pressure
    temp:
        A sub-daily timeseries of temperature

    Returns
    -------
    rh:
        A sub-daily timeseries of relative humidity
    """
    rh = (cnst.MAX_PERCENT * cnst.MBAR_PER_BAR
          * (vapor_pressure / svp(temp.values)))
    return rh.where(rh < cnst.MAX_PERCENT, cnst.MAX_PERCENT)


def vapor_pressure(vp_daily: pd.Series, temp: pd.Series,
                   t_t_min: np.array, n_out: int, ts: float):
    """
    Calculate vapor pressure.  First a linear interpolation
    of the daily values is calculated.  Then this is compared
    to the saturated vapor pressure calculated using the
    disaggregated temperature. When the interpolated vapor
    pressure is greater than the calculated saturated
    vapor pressure, the interpolation is replaced with the
    saturation value.

    Parameters
    ----------
    vp_daily:
        Daily vapor pressure
    temp:
        Sub-daily temperature
    t_t_min:
        Timeseries of minimum daily temperature
    n_out:
        Number of output observations
    ts:
        Timestep to disaggregate down to

    Returns
    -------
    vp:
        A sub-daily timeseries of the vapor pressure
    """
    # Linearly interpolate the values
    interp = scipy.interpolate.interp1d(t_t_min, vp_daily/cnst.MBAR_PER_BAR,
                                        fill_value='extrapolate')
    vp_disagg = interp(ts * np.arange(0, n_out))

    # Account for situations where vapor pressure is higher than
    # saturation point
    vp_sat = svp(temp.values) / cnst.MBAR_PER_BAR
    vp_disagg = np.where(vp_sat < vp_disagg, vp_sat, vp_disagg)
    return vp_disagg


def longwave(air_temp: pd.Series, vapor_pressure: pd.Series,
             tskc: pd.Series, params: dict):
    """
    Calculate longwave. This calculation can be performed
    using a variety of parameterizations for both the
    clear sky and cloud covered emissivity. Options for
    choosing these parameterizations should be passed in
    via the `params` argument.

    Parameters
    ----------
    air_temp:
        Sub-daily temperature
    vapor_pressure:
        Sub-daily vapor pressure
    tskc:
        Daily cloud fraction
    params:
        A dictionary of parameters, which contains
        information about which emissivity and cloud
        fraction methods to use.

    Returns
    -------
    (lwrad, tskc):
        A sub-daily timeseries of the longwave radiation
        as well as a sub-daily timeseries of the cloud
        cover fraction.
    """
    emissivity_calc = {
        'DEFAULT': lambda vp: vp,
        'TVA': lambda vp: 0.74 + 0.0049 * vp,
        'ANDERSON': lambda vp: 0.68 + 0.036 * np.power(vp, 0.5),
        'BRUTSAERT': lambda vp: 1.24 * np.power(vp / air_temp, 0.14285714),
        'SATTERLUND': lambda vp: 1.08 * (
            1 - np.exp(-1 * np.power(vp, (air_temp / 2016)))),
        'IDSO': lambda vp: 0.7 + 5.95e-5 * vp * np.exp(1500 / air_temp),
        'PRATA': lambda vp: (1 - (1 + (46.5*vp/air_temp)) * np.exp(
            -np.sqrt((1.2 + 3. * (46.5*vp / air_temp)))))
        }
    cloud_calc = {
        'DEFAULT': lambda emis: (1.0 + (0.17 * tskc ** 2)) * emis,
        'CLOUD_DEARDORFF': lambda emis: tskc + (1 - tskc) * emis
        }
    # Reindex and fill cloud cover, then convert temps to K
    tskc = tskc.reindex_like(air_temp).fillna(method='ffill')
    air_temp = air_temp + cnst.KELVIN
    vapor_pressure = vapor_pressure * 10

    # Calculate longwave radiation based on the options
    emiss_func = emissivity_calc[params['lw_type'].upper()]
    emissivity_clear = emiss_func(vapor_pressure)
    emiss_func = cloud_calc[params['lw_cloud'].upper()]
    emissivity = emiss_func(emissivity_clear)
    lwrad = emissivity * cnst.STEFAN_B * np.power(air_temp, 4)
    return lwrad, tskc


def shortwave(sw_rad: pd.Series, daylength: pd.Series, day_of_year: pd.Series,
              tiny_rad_fract: np.array, params: dict):
    """
    Disaggregate shortwave radiation down to a subdaily timeseries.

    Parameters
    ----------
    sw_rad:
        Daily incoming shortwave radiation
    daylength:
        List of daylength time for each day of year
    day_of_year:
        Timeseries of index of days since Jan-1
    tiny_rad_fract:
        Fraction of the daily potential radiation
        during a radiation time step defined by SW_RAD_DT
    params:
        Dictionary of parameters from the MetSim object

    Returns
    -------
    disaggrad:
        A sub-daily timeseries of shortwave radiation.
    """
    tiny_step_per_hour = cnst.SEC_PER_HOUR / cnst.SW_RAD_DT
    tmp_rad = sw_rad * daylength / cnst.SEC_PER_HOUR
    n_days = len(tmp_rad)
    ts_per_day = (cnst.HOURS_PER_DAY *
                  cnst.MIN_PER_HOUR / int(params['time_step']))
    disaggrad = np.zeros(int(n_days*ts_per_day))
    tiny_offset = ((params.get("theta_l", 0) - params.get("theta_s", 0)
                   / (cnst.HOURS_PER_DAY / cnst.DEG_PER_REV)))

    # Tinystep represents a daily set of values - but is constant across days
    tinystep = np.arange(cnst.HOURS_PER_DAY * tiny_step_per_hour) - tiny_offset
    inds = np.asarray(tinystep < 0)
    tinystep[inds] += cnst.HOURS_PER_DAY * tiny_step_per_hour
    inds = np.asarray(tinystep > (cnst.HOURS_PER_DAY * tiny_step_per_hour-1))
    tinystep[inds] -= (cnst.HOURS_PER_DAY * tiny_step_per_hour)
    tinystep = np.asarray(tinystep, dtype=np.int32)

    # Chunk sum takes in the distribution of radiation throughout the day
    # and collapses it into chunks that correspond to the desired timestep
    chunk_size = int(int(params['time_step'])
                     * (cnst.SEC_PER_MIN / cnst.SW_RAD_DT))

    # Chunk sum takes in the distribution of radiation throughout the day
    # and collapses it into chunks that correspond to the desired timestep
    def chunk_sum(x):
        return np.sum(x.reshape((int(len(x)/chunk_size), chunk_size)), axis=1)

    for day in range(n_days):
        rad = tiny_rad_fract[day_of_year[day] - 1]
        dslice = slice(int(day * ts_per_day), int((day + 1) * ts_per_day))
        rad_chunk = rad[np.asarray(tinystep, dtype=np.int32)]
        disaggrad[dslice] = chunk_sum(rad_chunk) * tmp_rad[day]
    return disaggrad
