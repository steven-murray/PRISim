from __future__ import division
import numpy as NP
import progressbar as PGB
import aipy as AP
import astropy 
from astropy.io import fits
from distutils.version import LooseVersion
import my_DSP_modules as DSP 
import baseline_delay_horizon as DLY
import geometry as GEOM
import interferometry as RI

#################################################################################

def _astropy_columns(cols, tabtype='BinTableHDU'):
    
    """
    ----------------------------------------------------------------------------
    !!! FOR INTERNAL USE ONLY !!!
    This internal routine checks for Astropy version and produces the FITS 
    columns based on the version

    Inputs:

    cols    [list of Astropy FITS columns] These are a list of Astropy FITS 
            columns

    tabtype [string] specifies table type - 'BinTableHDU' (default) for binary
            tables and 'TableHDU' for ASCII tables

    Outputs:

    columns [Astropy FITS column data] 
    ----------------------------------------------------------------------------
    """

    try:
        cols
    except NameError:
        raise NameError('Input cols not specified')

    if tabtype not in ['BinTableHDU', 'TableHDU']:
        raise ValueError('tabtype specified is invalid.')

    use_ascii = False
    if tabtype == 'TableHDU':
        use_ascii = True
    if astropy.__version__ == '0.4':
        columns = fits.ColDefs(cols, tbtype=tabtype)
    elif LooseVersion(astropy.__version__)>=LooseVersion('0.4.2'):
        columns = fits.ColDefs(cols, ascii=use_ascii)
    return columns    

################################################################################

def _gentle_clean(dd, _w, tol=1e-1, area=None, stop_if_div=True, maxiter=100,
                  verbose=False, autoscale=True):

    if verbose:
        print "Performing gentle clean..."

    scale_factor = 1.0
    if autoscale:
        scale_factor = NP.nanmax(NP.abs(_w))
    dd /= scale_factor
    _w /= scale_factor

    cc, info = AP.deconv.clean(dd, _w, tol=tol, area=area, stop_if_div=False,
                               maxiter=maxiter, verbose=verbose)
    #dd = info['res']

    cc = NP.zeros_like(dd)
    inside_res = NP.std(dd[area!=0])
    outside_res = NP.std(dd[area==0])
    initial_res = inside_res
    #print inside_res,'->',
    ncycle=0
    if verbose:
        print "inside_res outside_res"
        print inside_res, outside_res
    inside_res = 2*outside_res #just artifically bump up the inside res so the loop runs at least once
    while(inside_res>outside_res and maxiter>0):
        if verbose: print '.',
        _d_cl, info = AP.deconv.clean(dd, _w, tol=tol, area=area, stop_if_div=stop_if_div, maxiter=maxiter, verbose=verbose, pos_def=True)
        res = info['res']
        inside_res = NP.std(res[area!=0])
        outside_res = NP.std(res[area==0])
        dd = info['res']
        cc += _d_cl
        ncycle += 1
        if verbose: print inside_res*scale_factor, outside_res*scale_factor
        if ncycle>1000: break

    info['ncycle'] = ncycle-1

    dd *= scale_factor
    _w *= scale_factor
    cc *= scale_factor
    info['initial_residual'] = initial_res * scale_factor
    info['final_residual'] = inside_res * scale_factor
    
    return cc, info

#################################################################################

class DelaySpectrum(object):

    """
    ----------------------------------------------------------------------------
    Class to manage information on a multi-element interferometer array. 

    Attributes:

    ia          [instance of class InterferometerArray] An instance of class
                InterferometerArray that contains the results of the simulated
                interferometer visibilities

    baselines:  [M x 3 Numpy array] The baseline vectors associated with the
                M interferometers in SI units. The coordinate system of these
                vectors is specified by another attribute baseline_coords. 

    baseline_coords
                [string] Coordinate system for the baseline vectors. Default is 
                'localenu'. Other accepted values are 'equatorial' 

    baseline_lengths
                [M-element numpy array] Lengths of the baseline in SI units

    bp          [numpy array] Bandpass weights of size n_baselines x nchan x
                n_acc, where n_acc is the number of accumulations in the
                observation, nchan is the number of frequency channels, and
                n_baselines is the number of baselines

    bp_wts      [numpy array] Additional weighting to be applied to the bandpass
                shapes during the application of the member function 
                delay_transform(). Same size as attribute bp. 

    f           [list or numpy vector] frequency channels in Hz

    df          [scalar] Frequency resolution (in Hz)

    lags        [numpy vector] Time axis obtained when the frequency axis is
                inverted using a FFT. Same size as channels. This is 
                computed in member function delay_transform().

    lag_kernel  [numpy array] Inverse Fourier Transform of the frequency 
                bandpass shape. In other words, it is the impulse response 
                corresponding to frequency bandpass. Same size as attributes
                bp and bp_wts. It is initialized in __init__() member function
                but effectively computed in member function delay_transform()

    lst         [list] List of LST (in degrees) for each timestamp

    n_acc       [scalar] Number of accumulations

    pointing_center
                [2-column numpy array] Pointing center (latitude and 
                longitude) of the observation at a given timestamp. This is 
                where the telescopes will be phased up to as reference. 
                Coordinate system for the pointing_center is specified by another 
                attribute pointing_coords.

    phase_center
                [2-column numpy array] Phase center (latitude and 
                longitude) of the observation at a given timestamp. This is 
                where the telescopes will be phased up to as reference. 
                Coordinate system for the phase_center is specified by another 
                attribute phase_center_coords.

    pointing_coords
                [string] Coordinate system for telescope pointing. Accepted 
                values are 'radec' (RA-Dec), 'hadec' (HA-Dec) or 'altaz' 
                (Altitude-Azimuth). Default = 'hadec'.

    phase_center_coords
                [string] Coordinate system for array phase center. Accepted 
                values are 'radec' (RA-Dec), 'hadec' (HA-Dec) or 'altaz' 
                (Altitude-Azimuth). Default = 'hadec'.

    horizon_delay_limits
                [numpy array] NxMx2 numpy array denoting the neagtive and 
                positive horizon delay limits where N is the number of 
                timestamps, M is the number of baselines. The 0 index in the 
                third dimenstion denotes the negative horizon delay limit while 
                the 1 index denotes the positive horizon delay limit

    skyvis_lag  [numpy array] Complex visibility due to sky emission (in Jy Hz or
                K Hz) along the delay axis for each interferometer obtained by
                FFT of skyvis_freq along frequency axis. Same size as vis_freq.
                Created in the member function delay_transform(). Read its
                docstring for more details. Same dimensions as skyvis_freq

    vis_lag     [numpy array] The simulated complex visibility (in Jy Hz or K Hz) 
                along delay axis for each interferometer obtained by FFT of
                vis_freq along frequency axis. Same size as vis_noise_lag and
                skyis_lag. It is evaluated in member function delay_transform(). 

    vis_noise_lag
                [numpy array] Complex visibility noise (in Jy Hz or K Hz) along 
                delay axis for each interferometer generated using an FFT of
                vis_noise_freq along frequency axis. Same size as vis_noise_freq.
                Created in the member function delay_transform(). Read its
                docstring for more details. 

    cc_skyvis_lag
                [numpy array] Complex cleaned visibility delay spectra (in 
                Jy Hz or K Hz) of noiseless simulated sky visibilities for each 
                baseline at each LST. Size is nbl x nlags x nlst

    cc_skyvis_res_lag
                [numpy array] Complex residuals from cleaned visibility delay 
                spectra (in Jy Hz or K Hz) of noiseless simulated sky 
                visibilities for each baseline at each LST. Size is 
                nbl x nlags x nlst

    cc_skyvis_net_lag
                [numpy array] Sum of complex cleaned visibility delay spectra
                and residuals (in Jy Hz or K Hz) of noiseless simulated sky 
                visibilities for each baseline at each LST. Size is 
                nbl x nlags x nlst. cc_skyvis_net_lag = cc_skyvis_lag + 
                cc_skyvis_res_lag

    cc_vis_lag
                [numpy array] Complex cleaned visibility delay spectra (in 
                Jy Hz or K Hz) of noisy simulated sky visibilities for each 
                baseline at each LST. Size is nbl x nlags x nlst

    cc_vis_res_lag
                [numpy array] Complex residuals from cleaned visibility delay 
                spectra (in Jy Hz or K Hz) of noisy simulated sky 
                visibilities for each baseline at each LST. Size is 
                nbl x nlags x nlst

    cc_vis_net_lag
                [numpy array] Sum of complex cleaned visibility delay spectra
                and residuals (in Jy Hz or K Hz) of noisy simulated sky 
                visibilities for each baseline at each LST. Size is 
                nbl x nlags x nlst. cc_vis_net_lag = cc_vis_lag + 
                cc_vis_res_lag

    cc_skyvis_freq
                [numpy array] Complex cleaned visibility delay spectra 
                transformed to frequency domain (in Jy or K.Sr) obtained from 
                noiseless simulated sky visibilities for each baseline at each 
                LST. Size is nbl x nlags x nlst

    cc_skyvis_res_freq
                [numpy array] Complex residuals from cleaned visibility delay 
                spectra transformed to frequency domain (in Jy or K.Sr) obtained 
                from noiseless simulated sky visibilities for each baseline at 
                each LST. Size is nbl x nlags x nlst

    cc_skyvis_net_freq
                [numpy array] Sum of complex cleaned visibility delay spectra
                and residuals transformed to frequency domain (in Jy or K.Sr) 
                obtained from noiseless simulated sky visibilities for each 
                baseline at each LST. Size is nbl x nlags x nlst. 
                cc_skyvis_net_freq = cc_skyvis_freq + cc_skyvis_res_freq

    cc_vis_freq
                [numpy array] Complex cleaned visibility delay spectra 
                transformed to frequency domain (in Jy or K.Sr) obtained from 
                noisy simulated sky visibilities for each baseline at each LST. 
                Size is nbl x nlags x nlst

    cc_vis_res_freq
                [numpy array] Complex residuals from cleaned visibility delay 
                spectra transformed to frequency domain (in Jy or K.Sr) of noisy 
                simulated sky visibilities for each baseline at each LST. Size 
                is nbl x nlags x nlst

    cc_vis_net_freq
                [numpy array] Sum of complex cleaned visibility delay spectra
                and residuals transformed to frequency domain (in Jy or K.Sr) 
                obtained from noisy simulated sky visibilities for each baseline 
                at each LST. Size is nbl x nlags x nlst. 
                cc_vis_net_freq = cc_vis_freq + cc_vis_res_freq

    clean_window_buffer
                [scalar] number of inverse bandwidths to extend beyond the 
                horizon delay limit to include in the CLEAN deconvolution. 

    pad         [scalar] Non-negative scalar indicating padding fraction 
                relative to the number of frequency channels. For e.g., a 
                pad of 1.0 pads the frequency axis with zeros of the same 
                width as the number of channels. After the delay transform,
                the transformed visibilities are downsampled by a factor of
                1+pad. If a negative value is specified, delay transform 
                will be performed with no padding

    timestamp   [list] List of timestamps during the observation

    Member functions:

    __init__()  Initializes an instance of class DelaySpectrum
                        
    delay_transform()  
                Transforms the visibilities from frequency axis onto 
                delay (time) axis using an IFFT. This is performed for 
                noiseless sky visibilities, thermal noise in visibilities, 
                and observed visibilities. 

    clean()     Transforms the visibilities from frequency axis onto delay 
                (time) axis using an IFFT and deconvolves the delay transform 
                quantities along the delay axis. This is performed for noiseless 
                sky visibilities, thermal noise in visibilities, and observed 
                visibilities. 

    get_horizon_delay_limits()
                Estimates the delay envelope determined by the sky horizon 
                for the baseline(s) for the phase centers 

    set_horizon_delay_limits()
                Estimates the delay envelope determined by the sky horizon for 
                the baseline(s) for the phase centers of the DelaySpectrum 
                instance. No output is returned. Uses the member function 
                get_horizon_delay_limits()

    save()      Saves the interferometer array delay spectrum information to 
                disk. 
    ----------------------------------------------------------------------------
    """

    def __init__(self, interferometer_array):

        """
        ------------------------------------------------------------------------
        Intialize the DelaySpectrum class which manages information on delay
        spectrum of a multi-element interferometer.

        Class attributes initialized are:
        baselines, f, pointing_coords, baseline_coords, baseline_lengths, 
        bp, bp_wts, df, lags, lst, pointing_center, skyvis_lag, timestamp, 
        vis_lag, n_acc, vis_noise_lag, ia, pad, lag_kernel, 
        horizon_delay_limits, cc_skyvis_lag, cc_skyvis_res_lag, 
        cc_skyvis_net_lag, cc_vis_lag, cc_vis_res_lag, cc_vis_net_lag, 
        cc_skyvis_freq, cc_skyvis_res_freq, cc_sktvis_net_freq, cc_vis_freq,
        cc_vis_res_freq, cc_vis_net_freq, clean_window_buffer

        Read docstring of class DelaySpectrum for details on these
        attributes.

        Input(s):

        interferometer_array
                     [instance of class InterferometerArray] An instance of 
                     class InterferometerArray from which certain attributes 
                     will be obtained and used

        Other input parameters have their usual meanings. Read the docstring of
        class DelaySpectrum for details on these inputs.
        ------------------------------------------------------------------------
        """
        
        try:
            interferometer_array
        except NameError:
            raise NameError('Inpute interfeomter_array is not specified')

        if not isinstance(interferometer_array, RI.InterferometerArray):
            raise TypeError('Input interferometer_array must be an instance of class InterferometerArray')

        self.ia = interferometer_array
        self.f = interferometer_array.channels
        self.df = interferometer_array.freq_resolution
        self.baselines = interferometer_array.baselines
        self.baseline_lengths = interferometer_array.baseline_lengths
        self.baseline_coords = interferometer_array.baseline_coords
        self.phase_center = interferometer_array.phase_center
        self.phase_center_coords = interferometer_array.phase_center_coords
        self.pointing_center = interferometer_array.pointing_center
        self.pointing_coords = interferometer_array.pointing_coords
        self.lst = interferometer_array.lst
        self.timestamp = interferometer_array.timestamp
        self.n_acc = interferometer_array.n_acc
        self.horizon_delay_limits = self.get_horizon_delay_limits()

        self.bp = interferometer_array.bp # Inherent bandpass shape
        self.bp_wts = interferometer_array.bp_wts # Additional bandpass weights

        self.pad = 0.0
        self.lags = None
        self.lag_kernel = None

        self.skyvis_lag = None
        self.vis_lag = None
        self.vis_noise_lag = None

        self.vis_freq = None
        self.skyvis_freq = None
        self.vis_noise_freq = None

        self.clean_window_buffer = 1.0

        self.cc_skyvis_lag = None
        self.cc_skyvis_res_lag = None
        self.cc_vis_lag = None
        self.cc_vis_res_lag = None

        self.cc_skyvis_net_lag = None
        self.cc_vis_net_lag = None

        self.cc_skyvis_freq = None
        self.cc_skyvis_res_freq = None
        self.cc_vis_freq = None
        self.cc_vis_res_freq = None

        self.cc_skyvis_net_freq = None
        self.cc_vis_net_freq = None

    #############################################################################

    def delay_transform(self, pad=1.0, freq_wts=None, downsample=True,
                        verbose=True):

        """
        ------------------------------------------------------------------------
        Transforms the visibilities from frequency axis onto delay (time) axis
        using an IFFT. This is performed for noiseless sky visibilities, thermal
        noise in visibilities, and observed visibilities. 

        Inputs:

        pad         [scalar] Non-negative scalar indicating padding fraction 
                    relative to the number of frequency channels. For e.g., a 
                    pad of 1.0 pads the frequency axis with zeros of the same 
                    width as the number of channels. After the delay transform,
                    the transformed visibilities are downsampled by a factor of
                    1+pad. If a negative value is specified, delay transform 
                    will be performed with no padding

        freq_wts    [numpy vector or array] window shaping to be applied before
                    computing delay transform. It can either be a vector or size
                    equal to the number of channels (which will be applied to all
                    time instances for all baselines), or a nchan x n_snapshots 
                    numpy array which will be applied to all baselines, or a 
                    n_baselines x nchan numpy array which will be applied to all 
                    timestamps, or a n_baselines x nchan x n_snapshots numpy 
                    array. Default (None) will not apply windowing and only the
                    inherent bandpass will be used.

        downsample  [boolean] If set to True (default), the delay transform
                    quantities will be downsampled by exactly the same factor
                    that was used in padding. For instance, if pad is set to 
                    1.0, the downsampling will be by a factor of 2. If set to 
                    False, no downsampling will be done even if the original 
                    quantities were padded 

        verbose     [boolean] If set to True (default), print diagnostic and 
                    progress messages. If set to False, no such messages are
                    printed.
        ------------------------------------------------------------------------
        """

        if verbose:
            print 'Preparing to compute delay transform...\n\tChecking input parameters for compatibility...'

        if not isinstance(pad, (int, float)):
            raise TypeError('pad fraction must be a scalar value.')
        if pad < 0.0:
            pad = 0.0
            if verbose:
                print '\tPad fraction found to be negative. Resetting to 0.0 (no padding will be applied).'

        if freq_wts is not None:
            if freq_wts.size == self.f.size:
                freq_wts = NP.repeat(NP.expand_dims(NP.repeat(freq_wts.reshape(1,-1), self.baselines.shape[0], axis=0), axis=2), self.n_acc, axis=2)
            elif freq_wts.size == self.f.size * self.n_acc:
                freq_wts = NP.repeat(NP.expand_dims(freq_wts.reshape(self.f.size, -1), axis=0), self.baselines.shape[0], axis=0)
            elif freq_wts.size == self.f.size * self.baselines.shape[0]:
                freq_wts = NP.repeat(NP.expand_dims(freq_wts.reshape(-1, self.f.size), axis=2), self.n_acc, axis=2)
            elif freq_wts.size == self.f.size * self.baselines.shape[0] * self.n_acc:
                freq_wts = freq_wts.reshape(self.baselines.shape[0], self.f.size, self.n_acc)
            else:
                raise ValueError('window shape dimensions incompatible with number of channels and/or number of tiemstamps.')
            self.bp_wts = freq_wts
            if verbose:
                print '\tFrequency window weights assigned.'

        if not isinstance(downsample, bool):
            raise TypeError('Input downsample must be of boolean type')

        if verbose:
            print '\tInput parameters have been verified to be compatible.\n\tProceeding to compute delay transform.'
            
        self.lags = DSP.spectral_axis(int(self.f.size*(1+pad)), delx=self.df, use_real=False, shift=True)
        if pad == 0.0:
            self.vis_lag = DSP.FT1D(self.ia.vis_freq * self.bp * self.bp_wts, ax=1, inverse=True, use_real=False, shift=True) * self.f.size * self.df
            self.skyvis_lag = DSP.FT1D(self.ia.skyvis_freq * self.bp * self.bp_wts, ax=1, inverse=True, use_real=False, shift=True) * self.f.size * self.df
            self.vis_noise_lag = DSP.FT1D(self.ia.vis_noise_freq * self.bp * self.bp_wts, ax=1, inverse=True, use_real=False, shift=True) * self.f.size * self.df
            self.lag_kernel = DSP.FT1D(self.bp * self.bp_wts, ax=1, inverse=True, use_real=False, shift=True) * self.f.size * self.df
            if verbose:
                print '\tDelay transform computed without padding.'
        else:
            npad = int(self.f.size * pad)
            self.vis_lag = DSP.FT1D(NP.pad(self.ia.vis_freq * self.bp * self.bp_wts, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=True) * (npad + self.f.size) * self.df
            self.skyvis_lag = DSP.FT1D(NP.pad(self.ia.skyvis_freq * self.bp * self.bp_wts, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=True) * (npad + self.f.size) * self.df
            self.vis_noise_lag = DSP.FT1D(NP.pad(self.ia.vis_noise_freq * self.bp * self.bp_wts, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=True) * (npad + self.f.size) * self.df
            self.lag_kernel = DSP.FT1D(NP.pad(self.bp * self.bp_wts, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=True) * (npad + self.f.size) * self.df

            if verbose:
                print '\tDelay transform computed with padding fraction {0:.1f}'.format(pad)
        if downsample:
            self.vis_lag = DSP.downsampler(self.vis_lag, 1+pad, axis=1)
            self.skyvis_lag = DSP.downsampler(self.skyvis_lag, 1+pad, axis=1)
            self.vis_noise_lag = DSP.downsampler(self.vis_noise_lag, 1+pad, axis=1)
            self.lag_kernel = DSP.downsampler(self.lag_kernel, 1+pad, axis=1)
            self.lags = DSP.downsampler(self.lags, 1+pad)
            self.lags = self.lags.flatten()
            if verbose:
                print '\tDelay transform products downsampled by factor of {0:.1f}'.format(1+pad)
                print 'delay_transform() completed successfully.'

        self.pad = pad

    #############################################################################
        
    def clean(self, pad=1.0, freq_wts=None, clean_window_buffer=1.0,
              verbose=True):

        """
        ------------------------------------------------------------------------
        Transforms the visibilities from frequency axis onto delay (time) axis
        using an IFFT and deconvolves the delay transform quantities along the 
        delay axis. This is performed for noiseless sky visibilities, thermal
        noise in visibilities, and observed visibilities. 

        Inputs:

        pad         [scalar] Non-negative scalar indicating padding fraction 
                    relative to the number of frequency channels. For e.g., a 
                    pad of 1.0 pads the frequency axis with zeros of the same 
                    width as the number of channels. If a negative value is 
                    specified, delay transform will be performed with no padding

        freq_wts    [numpy vector or array] window shaping to be applied before
                    computing delay transform. It can either be a vector or size
                    equal to the number of channels (which will be applied to all
                    time instances for all baselines), or a nchan x n_snapshots 
                    numpy array which will be applied to all baselines, or a 
                    n_baselines x nchan numpy array which will be applied to all 
                    timestamps, or a n_baselines x nchan x n_snapshots numpy 
                    array. Default (None) will not apply windowing and only the
                    inherent bandpass will be used.

        verbose     [boolean] If set to True (default), print diagnostic and 
                    progress messages. If set to False, no such messages are
                    printed.
        ------------------------------------------------------------------------
        """

        if not isinstance(pad, (int, float)):
            raise TypeError('pad fraction must be a scalar value.')
        if pad < 0.0:
            pad = 0.0
            if verbose:
                print '\tPad fraction found to be negative. Resetting to 0.0 (no padding will be applied).'
    
        bw = self.df * self.f.size
        pc = self.ia.phase_center
        pc_coords = self.ia.phase_center_coords
        if pc_coords == 'hadec':
            pc_altaz = GEOM.hadec2altaz(pc, self.ia.latitude, units='degrees')
            pc_dircos = GEOM.altaz2dircos(pc_altaz, units='degrees')
        elif pc_coords == 'altaz':
            pc_dircos = GEOM.altaz2dircos(pc, units='degrees')
        
        npad = int(self.f.size * pad)
        lags = DSP.spectral_axis(self.f.size + npad, delx=self.df, use_real=False, shift=False)
        dlag = lags[1] - lags[0]
    
        clean_area = NP.zeros(self.f.size + npad, dtype=int)
        skyvis_lag = (npad + self.f.size) * self.df * DSP.FT1D(NP.pad(self.ia.skyvis_freq*self.bp*self.bp_wts, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=False)
        vis_lag = (npad + self.f.size) * self.df * DSP.FT1D(NP.pad(self.ia.vis_freq*self.bp*self.bp_wts, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=False)
        lag_kernel = (npad + self.f.size) * self.df * DSP.FT1D(NP.pad(self.bp, ((0,0),(0,npad),(0,0)), mode='constant'), ax=1, inverse=True, use_real=False, shift=False)
        
        ccomponents_noiseless = NP.zeros_like(skyvis_lag)
        ccres_noiseless = NP.zeros_like(skyvis_lag)
    
        ccomponents_noisy = NP.zeros_like(vis_lag)
        ccres_noisy = NP.zeros_like(vis_lag)
        
        for snap_iter in xrange(self.n_acc):
            progress = PGB.ProgressBar(widgets=[PGB.Percentage(), PGB.Bar(marker='-', left=' |', right='| '), PGB.Counter(), '/{0:0d} Baselines '.format(self.baselines.shape[0]), PGB.ETA()], maxval=self.baselines.shape[0]).start()
            # delay_matrix = DLY.delay_envelope(self.baselines, pc_dircos[snap_iter,:], units='mks')
            for bl_iter in xrange(self.baselines.shape[0]):
                clean_area[NP.logical_and(lags <= self.horizon_delay_limits[snap_iter,bl_iter,1]+clean_window_buffer/bw, lags >= self.horizon_delay_limits[snap_iter,bl_iter,0]-clean_window_buffer/bw)] = 1
                # clean_area[NP.logical_and(lags <= delay_matrix[0,bl_iter,0]+delay_matrix[0,bl_iter,1]+clean_window_buffer/bw, lags >= -delay_matrix[0,bl_iter,0]+delay_matrix[0,bl_iter,1]-clean_window_buffer/bw)] = 1
    
                cc_noiseless, info_noiseless = _gentle_clean(skyvis_lag[bl_iter,:,snap_iter], lag_kernel[bl_iter,:,snap_iter], area=clean_area, stop_if_div=False, verbose=False, autoscale=True)
                ccomponents_noiseless[bl_iter,:,snap_iter] = cc_noiseless
                ccres_noiseless[bl_iter,:,snap_iter] = info_noiseless['res']
    
                cc_noisy, info_noisy = _gentle_clean(vis_lag[bl_iter,:,snap_iter], lag_kernel[bl_iter,:,snap_iter], area=clean_area, stop_if_div=False, verbose=False, autoscale=True)
                ccomponents_noisy[bl_iter,:,snap_iter] = cc_noisy
                ccres_noisy[bl_iter,:,snap_iter] = info_noisy['res']
    
                progress.update(bl_iter+1)
            progress.finish()
    
        deta = lags[1] - lags[0]
        cc_skyvis = NP.fft.fft(ccomponents_noiseless, axis=1) * deta
        cc_skyvis_res = NP.fft.fft(ccres_noiseless, axis=1) * deta
    
        cc_vis = NP.fft.fft(ccomponents_noisy, axis=1) * deta
        cc_vis_res = NP.fft.fft(ccres_noisy, axis=1) * deta
    
        self.skyvis_lag = NP.fft.fftshift(skyvis_lag, axes=1)
        self.vis_lag = NP.fft.fftshift(vis_lag, axes=1)
        self.lag_kernel = NP.fft.fftshift(lag_kernel, axes=1)
        self.cc_skyvis_lag = NP.fft.fftshift(ccomponents_noiseless, axes=1)
        self.cc_skyvis_res_lag = NP.fft.fftshift(ccres_noiseless, axes=1)
        self.cc_vis_lag = NP.fft.fftshift(ccomponents_noisy, axes=1)
        self.cc_vis_res_lag = NP.fft.fftshift(ccres_noisy, axes=1)

        self.cc_skyvis_net_lag = self.cc_skyvis_lag + self.cc_skyvis_res_lag
        self.cc_vis_net_lag = self.cc_vis_lag + self.cc_vis_res_lag
        self.lags = NP.fft.fftshift(lags)

        self.cc_skyvis_freq = cc_skyvis
        self.cc_skyvis_res_freq = cc_skyvis_res
        self.cc_vis_freq = cc_vis
        self.cc_vis_res_freq = cc_vis_res

        self.cc_skyvis_net_freq = cc_skyvis + cc_skyvis_res
        self.cc_vis_net_freq = cc_vis + cc_vis_res

        self.clean_window_buffer = clean_window_buffer
        
    #############################################################################
        
    def get_horizon_delay_limits(self, phase_center=None,
                                 phase_center_coords=None):

        """
        -------------------------------------------------------------------------
        Estimates the delay envelope determined by the sky horizon for the 
        baseline(s) for the phase centers 
    
        Inputs:
    
        phase_center
                [numpy array] Phase center of the observation as 2-column or
                3-column numpy array. Two columns are used when it is specified
                in 'hadec' or 'altaz' coordinates as indicated by the input 
                phase_center_coords or by three columns when 'dircos' coordinates 
                are used. This is where the telescopes will be phased up to as 
                reference. Coordinate system for the phase_center is specified 
                by another input phase_center_coords. Default=None implies the 
                corresponding attribute from the DelaySpectrum instance is used.
                This is a Nx2 or Nx3 array

        phase_center_coords
                [string] Coordinate system for array phase center. Accepted 
                values are 'hadec' (HA-Dec), 'altaz' (Altitude-Azimuth) or
                'dircos' (direction cosines). Default=None implies the 
                corresponding attribute from the DelaySpectrum instance is used.

        Outputs:
        
        horizon_envelope: 
             NxMx2 matrix where M is the number of baselines and N is the number 
             of phase centers. horizon_envelope[:,:,0] contains the minimum delay 
             after accounting for (any) non-zenith phase center.
             horizon_envelope[:,:,1] contains the maximum delay after accounting 
             for (any) non-zenith phase center(s).
        -------------------------------------------------------------------------
        """

        if phase_center is None:
            phase_center = self.ia.phase_center
            phase_center_coords = self.ia.phase_center_coords

        if phase_center_coords not in ['hadec', 'altaz', 'dircos']:
            raise ValueError('Phase center coordinates must be "altaz", "hadec" or "dircos"')
        
        if phase_center_coords == 'hadec':
            pc_altaz = GEOM.hadec2altaz(phase_center, self.ia.latitude, units='degrees')
            pc_dircos = GEOM.altaz2dircos(pc_altaz, units='degrees')
        elif phase_center_coords == 'altaz':
            pc_dircos = GEOM.altaz2dircos(phase_center, units='degrees')
        elif phase_center_coords == 'dircos':
            pc_dircos = phase_center

        horizon_envelope = DLY.horizon_delay_limits(self.baselines, pc_dircos, units='mks')
        return horizon_envelope
        
    #############################################################################
        
    def set_horizon_delay_limits(self):

        """
        -------------------------------------------------------------------------
        Estimates the delay envelope determined by the sky horizon for the 
        baseline(s) for the phase centers of the DelaySpectrum instance. No 
        output is returned. Uses the member function get_horizon_delay_limits()
        -------------------------------------------------------------------------
        """

        self.horizon_delay_limits = self.get_horizon_delay_limits()
        
    #############################################################################
        
    def save(self, outfile, tabtype='BinTabelHDU', overwrite=False,
             verbose=True):

        """
        -------------------------------------------------------------------------
        Saves the interferometer array delay spectrum information to disk. 

        Inputs:

        outfile      [string] Filename with full path to be saved to. Will be
                     appended with '.fits' extension for the interferometer array
                     data and '.cc.fits' for delay spectrum data

        Keyword Input(s):

        tabtype      [string] indicates table type for one of the extensions in 
                     the FITS file. Allowed values are 'BinTableHDU' and 
                     'TableHDU' for binary and ascii tables respectively. Default 
                     is 'BinTableHDU'.
                     
        overwrite    [boolean] True indicates overwrite even if a file already 
                     exists. Default = False (does not overwrite)
                     
        verbose      [boolean] If True (default), prints diagnostic and progress
                     messages. If False, suppress printing such messages.
        -------------------------------------------------------------------------
        """

        try:
            outfile
        except NameError:
            raise NameError('No filename provided. Aborting DelaySpectrum.save()...')

        if verbose:
            print '\nSaving information about interferometer array...'

        self.ia.save(outfile, tabtype=tabtype, overwrite=overwrite,
                     verbose=verbose)

        if verbose:
            print '\nSaving information about delay spectra...'

        hdulist = []
        hdulist += [fits.PrimaryHDU()]
        hdulist[0].header['EXTNAME'] = 'PRIMARY'
        hdulist[0].header['NCHAN'] = (self.f.size, 'Number of frequency channels')
        hdulist[0].header['NLAGS'] = (self.lags.size, 'Number of lags')
        hdulist[0].header['freq_resolution'] = (self.df, 'Frequency resolution (Hz)')
        hdulist[0].header['N_ACC'] = (self.n_acc, 'Number of accumulations')
        hdulist[0].header['PAD'] = (self.pad, 'Padding factor')
        hdulist[0].header['DBUFFER'] = (self.clean_window_buffer, 'CLEAN window buffer (1/bandwidth)')
        hdulist[0].header['IARRAY'] = (outfile+'.fits', 'Location of InterferometerArray simulated visibilities')

        if verbose:
            print '\tCreated a primary HDU.'

        cols = []
        cols += [fits.Column(name='frequency', format='D', array=self.f)]
        cols += [fits.Column(name='lag', format='D', array=self.lags)]
        columns = _astropy_columns(cols, tabtype=tabtype)
        tbhdu = fits.new_table(columns)
        tbhdu.header.set('EXTNAME', 'SPECTRAL INFO')
        hdulist += [tbhdu]
        if verbose:
            print '\tCreated an extension for spectral information.'

        hdulist += [fits.ImageHDU(self.horizon_delay_limits, name='HORIZON LIMITS')]
        if verbose:
            print '\tCreated an extension for horizon delay limits of size {0[0]} x {0[1]} x {0[2]} as a function of snapshot instance, baseline, and (min,max) limits'.format(self.horizon_delay_limits.shape)

        hdulist += [fits.ImageHDU(self.bp_wts, name='BANDPASS WEIGHTS')]
        if verbose:
            print '\tCreated an extension for bandpass weights of size {0[0]} x {0[1]} x {0[2]} as a function of baseline,  frequency, and snapshot instance'.format(self.bp_wts.shape)

        hdulist += [fits.ImageHDU(self.lag_kernel.real, name='LAG KERNEL REAL')]
        hdulist += [fits.ImageHDU(self.lag_kernel.imag, name='LAG KERNEL IMAG')]
        if verbose:
            print '\tCreated an extension for convolving lag kernel of size {0[0]} x {0[1]} x {0[2]} as a function of baseline, lags, and snapshot instance'.format(self.lag_kernel.shape)
        
        hdulist += [fits.ImageHDU(self.cc_skyvis_lag.real, name='CLEAN NOISELESS DELAY SPECTRA REAL')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_lag.imag, name='CLEAN NOISELESS DELAY SPECTRA IMAG')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_res_lag.real, name='CLEAN NOISELESS DELAY SPECTRA RESIDUALS REAL')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_res_lag.imag, name='CLEAN NOISELESS DELAY SPECTRA RESIDUALS IMAG')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_freq.real, name='CLEAN NOISELESS VISIBILITIES REAL')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_freq.imag, name='CLEAN NOISELESS VISIBILITIES IMAG')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_res_freq.real, name='CLEAN NOISELESS VISIBILITIES RESIDUALS REAL')]
        hdulist += [fits.ImageHDU(self.cc_skyvis_res_freq.imag, name='CLEAN NOISELESS VISIBILITIES RESIDUALS IMAG')]

        hdulist += [fits.ImageHDU(self.cc_vis_lag.real, name='CLEAN NOISY DELAY SPECTRA REAL')]
        hdulist += [fits.ImageHDU(self.cc_vis_lag.imag, name='CLEAN NOISY DELAY SPECTRA IMAG')]
        hdulist += [fits.ImageHDU(self.cc_vis_res_lag.real, name='CLEAN NOISY DELAY SPECTRA RESIDUALS REAL')]
        hdulist += [fits.ImageHDU(self.cc_vis_res_lag.imag, name='CLEAN NOISY DELAY SPECTRA RESIDUALS IMAG')]
        hdulist += [fits.ImageHDU(self.cc_vis_freq.real, name='CLEAN NOISY VISIBILITIES REAL')]
        hdulist += [fits.ImageHDU(self.cc_vis_freq.imag, name='CLEAN NOISY VISIBILITIES IMAG')]
        hdulist += [fits.ImageHDU(self.cc_vis_res_freq.real, name='CLEAN NOISY VISIBILITIES RESIDUALS REAL')]
        hdulist += [fits.ImageHDU(self.cc_vis_res_freq.imag, name='CLEAN NOISY VISIBILITIES RESIDUALS IMAG')]
        
        if verbose:
            print '\tCreated extensions for clean components of noiseless, noisy and residuals of visibilities in frequency and delay coordinates of size {0[0]} x {0[1]} x {0[2]} as a function of baselines, lags/frequency and snapshot instance'.format(self.lag_kernel.shape)

        hdu = fits.HDUList(hdulist)
        hdu.writeto(outfile+'.cc.fits', clobber=overwrite)

    #############################################################################
        