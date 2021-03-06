
import os
from Settings import set_blackbox, set_zogy
# this needs to be done before numpy is imported in [zogy]
os.environ['OMP_NUM_THREADS'] = str(set_blackbox.nthread)

from zogy import *

import re   # Regular expression operations
import glob # Unix style pathname pattern expansion 
from multiprocessing import Pool, Manager, Lock, Queue
import datetime as dt 
from dateutil.tz import gettz
from astropy.stats import sigma_clipped_stats
from scipy import ndimage
import astroscrappy
from acstools.satdet import detsat, make_mask, update_dq
import shutil
from StringIO import StringIO
#from slackclient import SlackClient as sc
import ephem  
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import ctypes

__version__ = '0.7.2'

#def init(l):
#    global lock
#    lock = l
    
def run_blackbox (telescope=None, mode=None, date=None, read_path=None, slack=None):

    if set_zogy.timing:
        t_run_blackbox = time.time()

    # initialize logging
    ####################
        
    global q, logger
    q = Manager().Queue() #create queue for logging

    genlog = logging.getLogger() #create logger
    genlog.setLevel(logging.INFO) #set level of logger
    formatter = logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s") #set format of logger
    logging.Formatter.converter = time.gmtime #convert time in logger to UCT
    genlogfile = '{}/{}/{}_{}.log'.format(set_blackbox.log_dir, telescope, telescope,
                                          dt.datetime.now().strftime('%Y%m%d_%H%m%S'))
    filehandler = logging.FileHandler(genlogfile, 'w+') #create log file
    filehandler.setFormatter(formatter) #add format to log file
    genlog.addHandler(filehandler) #link log file to logger

    log_stream = StringIO() #create log stream for upload to slack
    streamhandler_slack = logging.StreamHandler(log_stream) #add log stream to logger
    streamhandler_slack.setFormatter(formatter) #add format to log stream
    genlog.addHandler(streamhandler_slack) #link logger to log stream
    logger = MyLogger(genlog,mode,log_stream,slack) #load logger handler

    q.put(logger.info('processing in {} mode'.format(mode)))
    q.put(logger.info('log file: {}'.format(genlogfile)))
    q.put(logger.info('number of processes: {}'.format(set_blackbox.nproc)))
    q.put(logger.info('number of threads: {}'.format(set_blackbox.nthread)))

    # [read_path] is assumed to be the full path to the raw image
    # directory; if not provided as input parameter, it is defined
    # using the input [date] with the function [get_path]
    if read_path is None:
        read_path, __ = get_path(telescope, date, 'read')
        q.put(logger.info('processing files from directory: {}'.format(read_path)))
    else:
        # if it is provided but does not exist, exit
        if not os.path.isdir(read_path):
            loggger.critical('[read_path] directory provided does not exist:\n{}'
                             .format(read_path))
            raise (SystemExit)
        
    # create global lock instance that can be used in [blackbox_reduce] for
    # certain blocks/functions to be accessed by one process at a time
    global lock
    lock = Lock()

    # start queue that will contain entries containing the reference
    # image header OBJECT and FILTER values, so that duplicate
    # reference building for the same object and filter by different
    # threads can be avoided
    global ref_ID_filt
    ref_ID_filt = Queue()

    if mode == 'day':

        # if in day mode, feed all bias, flat and science images (in
        # this order) to [blackbox_reduce] using multiprocessing
        filenames = sort_files(read_path, '*fits*')

        if set_blackbox.nproc==1 :

            # if only 1 process is requested, run it witout
            # multiprocessing; this will allow images to be shown on
            # the fly if [set_zogy.display] is set to True. In
            # multiprocessing mode this is not allowed (at least not a
            # macbook).            
            print ('running with single processor') #DP: added brackets
            for filename in filenames:
                result = blackbox_reduce(filename, telescope, mode, read_path)

        else:
                
            try:
                pool = Pool(set_blackbox.nproc)
                results = [pool.apply_async(blackbox_reduce, (filename, telescope,
                                                              mode, read_path))
                           for filename in filenames]
                output = [p.get() for p in results]
                q.put(logger.info(output))
                pool.close()
                pool.join()
            except Exception as e:
                q.put(logger.info(traceback.format_exc()))
                q.put(logger.error('exception was raised during [pool.apply_async]: {}'
                                   .format(e)))

    elif mode == 'night':
        # if in night mode, check if anythin changes in input directory
        # and if there is a new file, feed it to [blackbox_reduce]

        # create queue for submitting jobs
        queue = Queue()
        # create pool with given number of processes and queue feeding
        # into action function
        pool = Pool(set_blackbox.nproc, action, (queue,))

        # create and setup observer, but do not start just yet
        observer = Observer()
        observer.schedule(FileWatcher(queue, telescope, mode, read_path),
                          read_path, recursive=False)

        # glob any files already there
        filenames = sort_files(read_path, '*fits*')
        # loop through waiting files and add to pool
        for filename in filenames: 
            queue.put([filename, telescope, mode, read_path])

        # determine time of next sunrise
        obs = ephem.Observer()
        obs.lat = str(set_zogy.obs_lat)
        obs.long = str(set_zogy.obs_long)
        sunrise = obs.next_rising(ephem.Sun())

        # start observer
        observer.start()

        # keep monitoring [read_path] directory as long as:
        while ephem.now()-sunrise < ephem.hour:
            time.sleep(1)

        # night has finished, but finish queue if not empty yet
        while not queue.empty:
            time.sleep(1)

        # all done!
        q.put(logger.info('stopping time reached, exiting pipeline.'))
        observer.stop() #stop observer
        observer.join() #join observer
        logging.shutdown()
        raise SystemExit

        
    if set_zogy.timing:
        log_timing_memory (t0=t_run_blackbox, label='run_blackbox', log=genlog)


################################################################################
    
def blackbox_reduce (filename, telescope, mode, read_path):

    """Function that takes as input a single raw fits image and works to
       work through entire chain of reduction steps, from correcting
       for the gain and overscan to running ZOGY on the reduced image.

    """

    if set_zogy.timing:
        t_blackbox_reduce = time.time()

    # for night mode, the image needs to be moved out of the directory
    # that is being monitored immediately, for one thing because it
    # will first get unzipped, and the unzipped file will be
    # recognized by the watchdog as a new file, which is a problem
    if mode == 'night':

        if '.fz' in filename:
            ext = 1
        else:
            ext = 0
        # just read the header for the moment
        header = read_hdulist(filename, ext_header=ext)
        # and determine the raw data path (which is not necessarily the
        # same as the input [read_path])
        raw_path, __ = get_path(telescope, header['DATE-OBS'], 'read')

        # in night mode, [read_path] should not be the same as
        # [raw_path] because the images will be transferred to and
        # unpacked in [raw_path], which is problematic if that is the
        # same as the directory that is being monitored for new images
        if raw_path == read_path:
            q.put(logger.critical('in night mode, the directory [read_path] that '+
                                  'is being monitored should not be identical to '+
                                  'the standard [raw_path] directory: {}'
                                  .format(raw_path)))

        # move the image to [raw_path]
        src = filename
        dest = '{}/{}'.format(raw_path, filename.split('/')[-1])
        shutil.move(src, dest)

        # and let [filename] refer to the image in [raw_path]
        filename = dest


    # read in image data and header; unzip image first if needed
    data, header = read_hdulist(unzip(filename), ext_data=0, ext_header=0,
                                dtype='float32')

    # extend the header with some useful keywords
    result = set_header(header, filename)
    
    q.put(logger.info('processing {}'.format(filename)))


    # defining various paths and output file names
    ##############################################
    
    # define [write_path] using the header DATE-OBS
    write_path, date_eve = get_path(telescope, header['DATE-OBS'], 'write')
    make_dir (write_path)
    bias_path = '{}/bias'.format(write_path)
    make_dir (bias_path)
    flat_path = '{}/flat'.format(write_path)
    make_dir (flat_path)

    # UT date (yyyymmdd) and time (hhmmss)
    utdate, uttime = date_obs_get(header).split('_')

    # if output file already exists, do not bother to redo it
    path = {'bias': bias_path, 'flat': flat_path, 'object': write_path}
    # 'IMAGETYP' keyword in lower case
    imgtype = header['IMAGETYP'].lower()
    filt = header['FILTER']
    exptime = int(header['EXPTIME'])
    fits_out = '{}/{}_{}_{}.fits'.format(path[imgtype], telescope, utdate, uttime)
    if imgtype == 'flat':
        fits_out = fits_out.replace('.fits', '_{}.fits'.format(filt))

    if imgtype == 'object':
        # if 'FIELD_ID' keyword is present in the header, which
        # is the case for the test
        if 'FIELD_ID' in header:
            obj = header['FIELD_ID']
        else:
            obj = header['OBJECT']
        obj = ''.join(e for e in obj if e.isalnum() or e=='-' or e=='_')
        fits_out = fits_out.replace('.fits', '_red.fits')
        fits_out_mask = fits_out.replace('_red.fits', '_mask.fits')

        # and reference image
        ref_path = '{}/{}/{}'.format(set_blackbox.ref_dir, telescope, obj)
        make_dir (ref_path)
        ref_fits_out = '{}/{}_{}_red.fits'.format(ref_path, telescope, filt)
        ref_fits_out_mask = ref_fits_out.replace('_red.fits', '_mask.fits')

        if os.path.isfile(ref_fits_out):
            header_ref = read_hdulist(ref_fits_out, ext_header=0)
            utdate_ref, uttime_ref = date_obs_get(header_ref).split('_')
            if utdate_ref==utdate and uttime_ref==uttime:
                q.put(logger.warn ('this image {} is the current reference image; skipping'
                                   .format(fits_out.split('/')[-1])))
                return

            
    if os.path.isfile(fits_out):
        q.put(logger.warn ('corresponding reduced image {} already exist; skipping'
                           .format(fits_out.split('/')[-1])))
        return

    q.put(logger.info('\nprocessing {}'.format(filename)))
    #q.put(logger.info('-'*(len(filename)+11)))
    
    if imgtype == 'object':
        # prepare directory to store temporary files related to this
        # OBJECT image.  This is set to the tmp directory defined by
        # [set_blackbox.tmp_dir] with subdirectory [telescope] and
        # another subdirectory: the name of the reduced image without
        # the .fits extension.
        tmp_path = '{}/{}/{}'.format(set_blackbox.tmp_dir, telescope,
                                     fits_out.split('/')[-1].replace('.fits',''))
        make_dir (tmp_path, empty=True)

        
    # now that output filename is known, create a logger that will
    # append the log commands to [logfile]
    if imgtype != 'object':
        # for biases and flats
        logfile = fits_out.replace('.fits','.log')
    else:
        # for object files, prepare the logfile in [tmp_path]
        logfile = '{}/{}'.format(tmp_path, fits_out.split('/')[-1]
                                 .replace('.fits','.log'))
    global log
    log = create_log (logfile)

    # immediately write some info to the log
    log.info('processing {}'.format(filename))
    log.info('image type: {}, filter: {}, exptime: {}s'
             .format(imgtype, filt, exptime))

    log.info('write_path: {}'.format(write_path))
    log.info('bias_path: {}'.format(bias_path))
    log.info('flat_path: {}'.format(flat_path))
    if imgtype == 'object':
        log.info('tmp_path: {}'.format(tmp_path))
        log.info('ref_path: {}'.format(ref_path))
    

    # gain correction
    #################
    try:
        log.info('correcting for the gain')
        gain_processed = False
        data = gain_corr(data, header)
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [gain_corr]: {}'.format(e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [gain_corr]: {}'.format(e))
    else:
        gain_processed = True
        header['GAIN'] = (1, '[e-/ADU] effective gain all channels')
    # following line needs to be outside if/else statements
    header['GAIN-P'] = (gain_processed, 'corrected for gain?')

    if set_zogy.display:
        ds9_arrays(gain_cor=data)

    #args_in = [data, header}
    #args_out = data
    #proc_ok = try_func (gain_corr, args_in, args_out)
    #header['GAIN-P'] = (proc_ok, 'corrected for gain?')
    #if proc_ok:
    #    header['GAIN'] = (1, '[e-/ADU] effective gain all channels')

    
    # crosstalk correction
    ######################
    if imgtype == 'object':
        # not needed for biases or flats
        try: 
            log.info('correcting for the crosstalk')
            xtalk_processed = False
            data_old = xtalk_corr (data, set_blackbox.crosstalk_file)
        except Exception as e:
            q.put(logger.info(traceback.format_exc()))
            q.put(log.error('exception was raised during [xtalk_corr]: {}'.format(e)))
            log.info(traceback.format_exc())
            log.error('exception was raised during [xtalk_corr]: {}'.format(e))
        else:
            xtalk_processed = True
        # following line needs to be outside if/else statements
        header['XTALK-P'] = (xtalk_processed, 'corrected for crosstalk?')
        header['XTALK-F'] = (set_blackbox.crosstalk_file.strip('/')[-1], 'name crosstalk coefficients file')

        if set_zogy.display:
            ds9_arrays(Xtalk_cor=data)
            
            
    # PMV 2018/12/20: non-linearity correction is not yet done, but
    # still add these keywords to the header
    header['NONLIN-P'] = (False, 'corrected for non-linearity?')
    header['NONLIN-F'] = ('', 'name non-linearity correction file')


    # overscan correction
    #####################
    try: 
        log.info('correcting for the overscan')
        os_processed = False
        data = os_corr(data, header)
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [os_corr]: {}'.format(e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [os_corr]: {}'.format(e))
    else:
        os_processed = True
    # following line needs to be outside if/else statements
    header['OS-P'] = (os_processed, 'corrected for overscan?')


    if set_zogy.display:
        ds9_arrays(os_cor=data)


    # if IMAGETYP=bias, write [data] to fits and leave [blackbox_reduce]
    if imgtype == 'bias':
        fits.writeto(fits_out, data.astype('float32'), header, overwrite=True)
        return
        

    # master bias creation and subtraction
    ######################################
    try: 
        log.info('subtracting the master bias')
        mbias_processed = False
        lock.acquire()
        data = master_corr(data, header, None, bias_path, date_eve, filt, 'bias')
        lock.release()
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [mbias_corr]: {}'.format(e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [mbias_corr]: {}'.format(e))
    else:
        mbias_processed = True
    # following line needs to be outside if/else statements
    header['MBIAS-P'] = (mbias_processed, 'corrected for master bias?')

    
    # if IMAGETYP=flat, write [data] to fits and leave [blackbox_reduce]
    if imgtype == 'flat':
        fits.writeto(fits_out, data.astype('float32'), header, overwrite=True)
        return

    if set_zogy.display:
        ds9_arrays(bias_sub=data)

        
    # create initial mask array
    ###########################
    if imgtype == 'object':
        try: 
            log.info('preparing the initial mask')
            mask_processed = False
            data_mask, header_mask = mask_init (data, header)
        except Exception as e:
            q.put(logger.info(traceback.format_exc()))
            q.put(logger.error('exception was raised during [mask_init]: {}'.format(e)))
            log.info(traceback.format_exc())
            log.error('exception was raised during [mask_init]: {}'.format(e))
        else:
            mask_processed = True
        # following line needs to be outside if/else statements
        header['MASK-P'] = (mask_processed, 'mask image created?')


    if set_zogy.display:
        ds9_arrays(mask=data_mask)


    # master flat creation and correction
    #####################################
    try: 
        log.info('flatfielding')
        mflat_processed = False
        lock.acquire()
        data = master_corr(data, header, data_mask, flat_path, date_eve, filt, 'flat')
        lock.release()
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [mflat_corr]: {}'.format(e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [mflat_corr]: {}'.format(e))
    else:
        mflat_processed = True
    # following line needs to be outside if/else statements
    header['MFLAT-P'] = (mflat_processed, 'corrected for master flat?')

    
    # PMV 2018/12/20: fringe correction is not yet done, but
    # still add these keywords to the header
    header['MFRING-P'] = (False, 'corrected for master fringe map?')
    header['MFRING-F'] = ('', 'name of master fringe map applied')


    if set_zogy.display:
        ds9_arrays(flat_cor=data)
        data_precosmics = np.copy(data)


    # cosmic ray detection and correction
    #####################################
    try: 
        log.info('detecting cosmic rays')
        cosmics_processed = False
        data, data_mask = cosmics_corr(data, header, data_mask, header_mask)
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [cosmics_corr]: {}'.format(e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [cosmics_corr]: {}'.format(e))
    else:
        cosmics_processed = True
    # following line needs to be outside if/else statements
    header['COSMIC-P'] = (cosmics_processed, 'corrected for cosmic rays?')

    
    if set_zogy.display:
        ds9_arrays(data=data_precosmics, cosmic_cor=data, mask=data_mask)
        print (header['NCOSMICS']) #DP: added brackets
        

    # satellite trail detection
    ###########################
    try: 
        log.info('detecting satellite trails')
        sat_processed = False
        data_mask = sat_detect(data, header, data_mask, header_mask,
                               tmp_path)
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [sat_detect]: {}'.format(e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [sat_detect]: {}'.format(e))
    else:
        sat_processed = True
    # following line needs to be outside if/else statements
    header['SAT-P'] = (sat_processed, 'corrected for cosmic rays?')
    
    # add some more info to mask header
    result = mask_header(data_mask, header_mask)
    
    # write data and mask to output images in [tmp_path]
    log.info('writing reduced image and mask to {}'.format(tmp_path))
    new_fits = '{}/{}'.format(tmp_path, fits_out.split('/')[-1]) 
    new_fits_mask = new_fits.replace('_red.fits', '_mask.fits')
    fits.writeto(new_fits, data.astype('float32'), header, overwrite=True)
    fits.writeto(new_fits_mask, data_mask.astype('uint8'), header_mask,
                 overwrite=True)
    
    if set_zogy.display:
        ds9_arrays(mask=data_mask)
        print (header['NSATS']) #DP: added brackets

        
    # run zogy's [optimal_subtraction]
    ##################################
    try: 
        log.info ('running optimal image subtraction')
        zogy_processed = False
        
        # using the function [check_ref], check if the reference image
        # with the same header OBJECT and FILTER as the currently
        # processed image happens to be made right now, using a lock
        lock.acquire()

        # change to [tmp_path]; only necessary if making plots as
        # PSFEx is producing its diagnostic output fits and plots in
        # the current directory
        if set_zogy.make_plots:
            os.chdir(tmp_path)
        
        # this extra second is to provide a head start to the process
        # that is supposed to be making the reference image; that
        # process needs to add its OBJECT and FILTER to the queue
        # [ref_ID_filt] before the next process is calling [check_ref]
        time.sleep(1)
        ref_being_made = check_ref(ref_ID_filt, (obj, filt))
        log.info('is reference for same OBJECT and FILTER being_made now?: {}'
                 .format(ref_being_made))
        lock.release()
        
        if ref_being_made:
            # if reference in this filter is being made, let the affected
            # process wait until reference building is done
            if ref_being_made:
                while check_ref(ref_ID_filt, (obj, filt)):
                    log.info ('waiting for reference job to be finished for '+
                              'OBJECT: {}, FILTER: {}'.format(obj, filt))
                    time.sleep(5)
                log.info ('done waiting for reference job to be finished for '+
                          'OBJECT: {}, FILTER: {}'.format(obj, filt))
                    
        # lock the following block to allow only a single process to
        # execute the reference image creation
        #lock.acquire()

        # if ref image has not yet been processed:
        if not os.path.isfile(ref_fits_out):
#            refjob = True
#        else:
#            refjob = False
#        if refjob:
            
            # update [ref_ID_filt] queue with a tuple with this OBJECT
            # and FILTER combination
            ref_ID_filt.put((obj, filt))

            log.info('making ref image')

            log.info('new_fits: {}'.format(new_fits))
            log.info('new_fits_mask: {}'.format(new_fits_mask))

            result = optimal_subtraction(ref_fits=new_fits,
                                         ref_fits_mask=new_fits_mask,
                                         set_file='Settings.set_zogy',
                                         log=log, verbose=None,
                                         nthread=set_blackbox.nthread)

            if set_zogy.timing:
                log_timing_memory (t0=t_blackbox_reduce, label='blackbox_reduce', log=log)
                
            # copy selected output files to reference directory
            ref_base = ref_fits_out.split('_red.fits')[0]
            tmp_base = new_fits.split('_red.fits')[0]
            result = copy_files2keep(tmp_base, ref_base, set_blackbox.ref_2keep)

            # now that reference is built, remove this reference ID
            # and filter combination from the [ref_ID_filt] queue
            lock.acquire()
            result = check_ref(ref_ID_filt, (obj, filt), method='remove')
            lock.release()
            
        else:

         #lock.release()        
#        if not refjob:
            
            # make symbolic links to all files in the reference
            # directory with the same filter
            ref_files = glob.glob('{}/{}*{}*'.format(ref_path, telescope, filt))
            for ref_file in ref_files:
                os.symlink(ref_file, '{}/{}'.format(tmp_path, ref_file.split('/')[-1]))

            ref_fits = '{}/{}'.format(tmp_path, ref_fits_out.split('/')[-1])
            ref_fits_mask = '{}/{}'.format(tmp_path, ref_fits_out_mask.split('/')[-1])
                        
            log.info('new_fits: {}'.format(new_fits))
            log.info('new_fits_mask: {}'.format(new_fits_mask))
            log.info('ref_fits: {}'.format(ref_fits))
            log.info('ref_fits_mask: {}'.format(ref_fits_mask))
        
            result = optimal_subtraction(new_fits=new_fits,
                                         ref_fits=ref_fits,
                                         new_fits_mask=new_fits_mask,
                                         ref_fits_mask=ref_fits_mask,
                                         set_file='Settings.set_zogy',
                                         log=log, verbose=None,
                                         nthread=set_blackbox.nthread)

            if set_zogy.timing:
                log_timing_memory (t0=t_blackbox_reduce, label='blackbox_reduce', log=log)

            # copy selected output files to new directory
            new_base = fits_out.split('_red.fits')[0]
            tmp_base = new_fits.split('_red.fits')[0]
            result = copy_files2keep(tmp_base, new_base, set_blackbox.new_2keep)


        lock.acquire()
        # change to [run_dir]
        if set_zogy.make_plots:
            os.chdir(set_blackbox.run_dir)
        # and delete [tmp_path] if [set_blackbox.keep_tmp] not True
        if not set_blackbox.keep_tmp and os.path.isdir(tmp_path):
            shutil.rmtree(tmp_path)
        lock.release()
        
            
    except Exception as e:
        log.info(traceback.format_exc())
        log.error('exception was raised during [optimal_subtraction]: {}'.format(e))
    else:
        zogy_processed = True

        
    return
        

################################################################################

def check_ref (queue_ref, obj_filt, method=None):

    mycopy = []
    ref_being_made = False
    while True:
        try:
            elem = queue_ref.get(False)
        except:
            break
        else:
            mycopy.append(elem)

    for elem in mycopy:
        if elem == obj_filt:
            ref_being_made = True
            if method != 'remove':
                queue_ref.put(elem)
                time.sleep(0.1)

    return ref_being_made

                
################################################################################

def try_func (func, args_in, args_out):

    """Helper function to avoid duplication when executing the different
       functions."""

    func_name = func.__name__

    try: 
        log.info('executing [{}]'.format(func_name))
        proc_ok = False
        args[0] = func (args[1:])
    except Exception as e:
        q.put(logger.info(traceback.format_exc()))
        q.put(logger.error('exception was raised during [{}]: {}'
                           .format(func_name, e)))
        log.info(traceback.format_exc())
        log.error('exception was raised during [{}]: {}'
                  .format(func_name, e))
    else:
        proc_ok = True

    return proc_ok

    
################################################################################

def create_log (logfile):

    #log = logging.getLogger() #create logger
    #log.setLevel(logging.INFO) #set level of logger
    #formatter = logging.Formatter("%(asctime)s %(funcName)s %(lineno)d %(levelname)s %(message)s") #set format of logger
    #logging.Formatter.converter = time.gmtime #convert time in logger to UTC
    #filehandler = logging.FileHandler(fits_out.replace('.fits','.log'), 'w+') #create log file
    #filehandler.setFormatter(formatter) #add format to log file
    #log.addHandler(filehandler) #link log file to logger

    logFormatter = logging.Formatter('%(asctime)s.%(msecs)03d [%(levelname)s, %(process)s] '+
                                     '%(message)s [%(funcName)s, line %(lineno)d]',
                                     '%Y-%m-%dT%H:%M:%S')
    logging.Formatter.converter = time.gmtime #convert time in logger to UTC
    log = logging.getLogger()

    fileHandler = logging.FileHandler(logfile)
    fileHandler.setFormatter(logFormatter)
    fileHandler.setLevel(logging.INFO)
    log.addHandler(fileHandler)

    streamHandler = logging.StreamHandler()
    streamHandler.setFormatter(logFormatter)
    streamHandler.setLevel(logging.WARN)
    log.addHandler(streamHandler)

    return log
    

################################################################################

def make_dir(path, empty=False):

    """Function to make directory, which is locked to use by 1 process.
       If [empty] is True and the directory already exists, it will
       first be removed.
    """

    lock.acquire()
    # if already exists but needs to be empty, remove it first
    if os.path.isdir(path) and empty:
        shutil.rmtree(path)
    if not os.path.isdir(path):
        os.makedirs(path)
    lock.release()
    return


################################################################################

def copy_files2keep (tmp_base, dest_base, ext2keep):

    """Function to copy files with base name [tmp_base] and extensions
    [ext2keep] to files with base name [dest_base] with the same
    extensions. The base names should include the full path.
    """
    
    # list of all files starting with [tmp_base]
    tmpfiles = glob.glob('{}*'.format(tmp_base))
    # loop this list
    for tmpfile in tmpfiles:
        # determine extension of file 
        tmp_ext = tmpfile.split(tmp_base)[-1]
        # check if the extension is present in [ext2keep]
        for ext in ext2keep:
            if ext == tmpfile[-len(ext):]:
                destfile = '{}{}'.format(dest_base, tmp_ext)
                # if so, and the source and destination names are not
                # identical, go ahead and copy
                if tmpfile != destfile:
                    log.info('copying {} to {}'.format(tmpfile, destfile))
                    shutil.copyfile(tmpfile, destfile)

    return


################################################################################

def sat_detect (data, header, data_mask, header_mask, tmp_path):

    if set_zogy.timing:
        t = time.time()

    #bin data
    binned_data = data.reshape(np.shape(data)[0]/set_blackbox.sat_bin,set_blackbox.sat_bin,
                               np.shape(data)[1]/set_blackbox.sat_bin,set_blackbox.sat_bin).sum(3).sum(1)
    satellite_fitting = False

    for j in range(3):
        #write binned data to tmp file
        fits_binned_mask = ('{}/{}'.format(
            tmp_path, tmp_path.split('/')[-1].replace('_red','_binned_satmask.fits')))
        fits.writeto(fits_binned_mask, binned_data, overwrite=True)
        #detect satellite trails
        results, errors = detsat(fits_binned_mask, chips=[0], n_processes=set_blackbox.nthread,
                                 buf=40, sigma=3, h_thresh=0.2)
        #create satellite trail if found
        trail_coords = results[(fits_binned_mask,0)] 
        #continue if satellite trail found
        if len(trail_coords) > 0: 
            trail_segment = trail_coords[0]
            try: 
                #create satellite trail mask
                mask_binned = make_mask(fits_binned_mask, 0, trail_segment, sublen=5,
                                        pad=0, sigma=5, subwidth=5000).astype(np.uint8)
            except ValueError:
                #if error occurs, add comment
                print ('Warning: satellite trail found but could not be fitted for file {} and is not included in the mask.'
                       .format(unique_dir.split('/')[-1]))
                break
            satellite_fitting = True
            binned_data[mask_binned == 1] = np.median(binned_data)
            fits_old_mask = unique_dir+'/old_mask.fits'
            if os.path.isfile(fits_old_mask):
                old_mask = read_hdulist(fits_old_mask, ext_data=0)
                mask_binned = old_mask+mask_binned
            fits.writeto(fits_old_mask, mask_binned, overwrite=True)
        else:
            break
    if satellite_fitting == True:
        #unbin mask
        mask_sat = np.kron(mask_binned, np.ones((set_blackbox.sat_bin,set_blackbox.sat_bin))).astype(np.uint8)
        # add pixels affected by cosmic rays to [data_mask]
        data_mask[mask_sat==1] += set_zogy.mask_value['satellite trail']
        # determining number of trails; 2 pixels are considered from the
        # same trail also if they are only connected diagonally
        struct = np.ones((3,3), dtype=bool)
        __, nsats = ndimage.label(mask_sat, structure=struct)
        nsatpixels = np.sum(mask_sat)
    else:
        nsats = 0
        nsatpixels = 0

    header['NSATS'] = (nsats, 'number of satellite trails identified')

    if set_zogy.timing:
        log_timing_memory (t0=t, label='sat_detect', log=log)

    return data_mask

        
################################################################################

def cosmics_corr (data, header, data_mask, header_mask):

    if set_zogy.timing:
        t = time.time()

    satlevel_electrons = set_blackbox.satlevel*np.mean(set_blackbox.gain) 
    mask_cr, data = astroscrappy.detect_cosmics(
        data, inmask=(data_mask!=0), sigclip=set_blackbox.sigclip,
        sigfrac=set_blackbox.sigfrac, objlim=set_blackbox.objlim, niter=set_blackbox.niter,
        readnoise=header['RDNOISE'], satlevel=satlevel_electrons,
        cleantype='medmask')
    
    # from astroscrappy 'manual': To reproduce the most similar
    # behavior to the original LA Cosmic (written in IRAF), set inmask
    # = None, satlevel = np.inf, sepmed=False, cleantype='medmask',
    # and fsmode='median'.
    #mask_cr, data = astroscrappy.detect_cosmics(
    #    data, inmask=None, sigclip=set_blackbox.sigclip,
    #    sigfrac=set_blackbox.sigfrac, objlim=set_blackbox.objlim, niter=set_blackbox.niter,
    #    readnoise=header['RDNOISE'], satlevel=np.inf)
    #
    #print 'np.sum(data_mask!=0)', np.sum(data_mask!=0)
    #print 'np.sum(mask_cr)', np.sum(mask_cr)
    #print 'np.sum((mask_cr) & (data_mask==0))', np.sum((mask_cr) & (data_mask==0))
    
    # add pixels affected by cosmic rays to [data_mask]
    data_mask[mask_cr==1] += set_zogy.mask_value['cosmic ray']

    # determining number of cosmics; 2 pixels are considered from the
    # same cosmic also if they are only connected diagonally
    struct = np.ones((3,3), dtype=bool)
    __, ncosmics = ndimage.label(mask_cr, structure=struct)
    header['NCOSMICS'] = (ncosmics, 'number of cosmic rays identified')

    if set_zogy.timing:
        log_timing_memory (t0=t, label='cosmics_corr', log=log)

    return data, data_mask


################################################################################

def mask_init (data, header):

    """Function to create initial mask from the bad pixel mask (defining
       the bad and edge pixels), and pixels that are saturated and
       pixels connected to saturated pixels.

    """
    
    if set_zogy.timing:
        t = time.time()

    fits_bpm = unzip(set_blackbox.bad_pixel_mask)
    if os.path.isfile(fits_bpm):
        # if it exists, read it
        data_mask = read_hdulist(fits_bpm, ext_data=0)
    else:
        # if not, create uint8 array of zeros with same shape as
        # [data]
        data_mask = np.zeros(np.shape(data), dtype='uint8')

    # mask of pixels with non-finite values in [data]
    mask_infnan = ~np.isfinite(data)
    # replace those pixel values with zeros
    data[mask_infnan] = 0
    # and add them to [data_mask] with same value defined for 'bad' pixels
    # unless that pixel was already masked
    data_mask[(mask_infnan) & (data_mask==0)] += set_zogy.mask_value['bad']
    
    # identify saturated pixels
    satlevel_electrons = set_blackbox.satlevel*np.mean(set_blackbox.gain) 
    mask_sat = (data >= satlevel_electrons)
    # add them to the mask of edge and bad pixels
    data_mask[mask_sat] += set_zogy.mask_value['saturated']

    # and pixels connected to saturated pixels
    struct = np.ones((3,3), dtype=bool)
    mask_satconnect = ndimage.binary_dilation(mask_sat, structure=struct)
    # add them to the mask
    data_mask[(mask_satconnect) & (~mask_sat)] += set_zogy.mask_value['saturated-connected']

    # create initial mask header 
    header_mask = fits.Header()
    header_mask['SATURATE'] = (satlevel_electrons, '[e-] adopted saturation threshold')
    # also add this to the header of image itself
    header['SATURATE'] = (satlevel_electrons, '[e-] adopted saturation threshold')
    # rest of the mask header entries are added in one go using
    # function [mask_header] once all the reduction steps have
    # finished
    
    if set_zogy.timing:
        log_timing_memory (t0=t, label='mask_init', log=log)

    return data_mask.astype('uint8'), header_mask


################################################################################

def mask_header(data_mask, header_mask):

    """Function to add info from all reduction steps to mask header"""
    
    mask = {}
    text = {'bad': 'BP', 'edge': 'EP', 'saturated': 'SP',
            'saturated-connected': 'SCP', 'satellite trail': 'STP',
            'cosmic ray': 'CRP'}
    
    for mask_type in text.keys():
        value = set_zogy.mask_value[mask_type]
        mask[mask_type] = (data_mask & value == value)
        header_mask['M-{}'.format(text[mask_type])] = (
            True, '{} pixels included in mask?'.format(mask_type))
        header_mask['M-{}VAL'.format(text[mask_type])] = (
            value, 'value added to mask for {} pixels'.format(mask_type))
        header_mask['M-{}NUM'.format(text[mask_type])] = (
            np.sum(mask[mask_type]), 'number of {} pixels'.format(mask_type))
        
    return

    
################################################################################

def master_corr (data, header, data_mask, path, date_eve, filt, imtype):

    if set_zogy.timing:
        t = time.time()

    if imtype=='flat':
        fits_master = '{}/{}_{}_{}.fits'.format(path, imtype, date_eve, filt)
    elif imtype=='bias':
        fits_master = '{}/{}_{}.fits'.format(path, imtype, date_eve)

    log.info('fits_master: {}'.format(fits_master))
        
    if not os.path.isfile(unzip(fits_master)):

        # prepare master from files in [path]
        if imtype=='flat':
            file_list = sorted(glob.glob('{}/*_{}.fits*'.format(path, filt)))
        elif imtype=='bias':
            file_list = sorted(glob.glob('{}/*fits*'.format(path)))

        # initialize cube of images to be combined
        nfiles = np.shape(file_list)[0]

        # if there are too few frames to make tonight's master, look
        # for a nearby master flat instead
        if nfiles < 3:

            fits_master_close = get_closest_biasflat(date_eve, imtype, filt=filt)
            if fits_master_close is not None:

                fits_master_close = unzip(fits_master_close)
                print ('Warning: too few images available to produce master {}; instead using\n{}'
                       .format(imtype, fits_master_close))
                # create symbolic link so future files will automatically
                # use this as the master flat
                os.symlink(fits_master_close, fits_master)

            else:
                log.error('no alternative master {} found'.format(imtype))
                return data
                
        else:
            
            print ('making master {} in filter {}'.format(imtype, filt))

            # assuming that individual flats/biases have the same shape as the input data
            master_cube = np.zeros((nfiles, np.shape(data)), dtype='float32')

            # fill the cube
            for i_file, filename in enumerate(file_list):
                data_temp, header_temp = read_hdulist(file_list[i_file],
                                                        ext_data=0, ext_header=0)

                if imtype=='flat':
                    # divide by median over the region [set_blackbox.flat_norm_sec]
                    mean, std, median = clipped_stats(data_temp[set_blackbox.flat_norm_sec])
                    print ('flat name: {}, mean: {}, std: {}, median: {}'
                           .format(filename, mean, std, median))
                    master_cube[i_file] = data_temp / median
                    
                if i_file==0:
                    for key in header_temp.keys():
                        if 'BIASM' in key or 'RDN' in key:
                            del header_temp[key]
                    header_master = header_temp
                    
                if imtype=='flat':
                    comment = 'name reduced flat'
                elif imtype=='bias':
                    comment = 'name gain/os-corrected bias frame'

                header_master['{}{}'.format(imtype.upper(), i_file+1)] = (
                    filename.split('/')[-1], '{} {}'.format(comment, i_file+1))
                
                if 'ORIGFILE' in header_temp.keys():
                    header_master['{}OR{}'.format(imtype.upper(), i_file+1)] = (
                        header_temp['ORIGFILE'], 'name original {} {}'
                        .format(imtype, i_file+1))


            # determine the median
            master_median = np.median(master_cube, axis=0)

            # add some header keywords to the master flat
            if imtype=='flat':
                sec_temp = set_blackbox.flat_norm_sec
                value_temp = '[{}:{},{}:{}]'.format(sec_temp[0].start+1, sec_temp[0].stop+1,
                                                    sec_temp[1].start+1, sec_temp[1].stop+1) 
                header_master['STATSEC'] = (value_temp,
                                            'pre-defined statistics section [y1:y2,x1:x2]')
                header_master['SECMED'] = (np.median(flat_median[sec_temp]),
                                           '[e-] median master flat over STATSEC')
                header_master['SECSTD'] = (np.std(flat_median[sec_temp]),
                                           '[e-] sigma (STD) master flat over STATSEC')

                # for full image statistics, discard masked pixels
                mask_ok = (data_mask==0)
                header_master['FLATMED'] = (np.median(flat_median[mask_ok]),
                                            '[e-] median master flat')
                header_master['FLATSTD'] = (np.std(flat_median[mask_ok]),
                                            '[e-] sigma (STD) master flat')

            elif imtype=='bias':

                # add some header keywords to the master bias
                mean_master, std_master = clipped_stats(bias_median, get_median=False)
                header_master['BIASMEAN'] = (mean_master, '[e-] mean master bias')
                header_master['RDNOISE'] = (std_master, '[e-] sigma (STD) master bias')

                # including the means and standard deviations of the master
                # bias in the separate channels
                data_sec_red = set_blackbox.data_sec_red
                nchans = np.shape(data_sec_red)[0]
                mean_chan = np.zeros(nchans)
                std_chan = np.zeros(nchans)

                for i_chan in range(nchans):
                    data_chan = bias_median[data_sec_red[i_chan]]
                    mean_chan[i_chan], std_chan[i_chan] = clipped_stats(data_chan, get_median=False)
                for i_chan in range(nchans):
                    header_master['BIASM{}'.format(i_chan+1)] = (
                        mean_chan[i_chan], '[e-] channel {} mean master bias'.format(i_chan+1))
                for i_chan in range(nchans):
                    header_master['RDN{}'.format(i_chan+1)] = (
                        std_chan[i_chan], '[e-] channel {} sigma (STD) master bias'.format(i_chan+1))

            # write to output file
            fits.writeto(fits_master, master_median.astype('float32'), header_master,
                         overwrite=True)

            
    log.info('reading master {}'.format(imtype))
    master_median = read_hdulist(fits_master, ext_data=0)
    if os.path.islink(fits_master):
        master_name = os.readlink(fits_master)
    else:
        master_name = fits_master
    header['M{}-F'.format(imtype.upper())] = (
        master_name.split('/')[-1], 'name of master {} applied'.format(imtype))
    
    if imtype=='flat':
        # divide data by the normalised flat
        # do not consider pixels with zero values or edge pixels
        mask_ok = ((master_median != 0) & (data_mask != set_zogy.mask_value['edge']))
        data[mask_ok] /= master_median[mask_ok]
    elif imtype=='bias':
        # subtract from data
        data -= master_median
                
    if set_zogy.timing:
        log_timing_memory (t0=t, label='master_corr', log=log)

    return data


################################################################################

def mflat_corr(data, header, data_mask, flat_path, date_eve, filt):

    if set_zogy.timing:
        t = time.time()
        
    fits_mflat = '{}/flat_{}_{}.fits'.format(flat_path, date_eve, filt)
    if not os.path.isfile(unzip(fits_mflat)):

        # prepare master flat from flats in [flat_path]
        flat_list = sorted(glob.glob('{}/*_{}.fits*'.format(flat_path, filt)))

        # initialize cube of flats to be combined
        nflat = np.shape(flat_list)[0]

        # if there are too few bias frames to make tonight's master
        # flat, look for a nearby master flat instead
        if nflat < 3:

            fits_mflat_close = get_closest_biasflat(date_eve, 'flat', filt=filt)

            if fits_mflat_close is not None:

                fits_mflat_close = unzip(fits_mflat_close)
                print ('Warning: too few flats available to produce master flat; instead using\n{}'
                       .format(fits_mflat_close))
                # create symbolic link so future files will automatically
                # use this as the master flat
                os.symlink(fits_mflat_close, fits_mflat)

            else:
                log.error('no alternative master flat found')
                return data
                
        else:
            
            print ('making master flat in filter {}'.format(filt))

            # assuming that flats have the same shape as the input data
            ysize, xsize = np.shape(data)
            flat_cube = np.zeros((nflat, ysize, xsize), dtype='float32')

            # fill the cube
            for i_flat, flat in enumerate(flat_list):
                flat_temp, header_temp = read_hdulist(flat_list[i_flat],
                                                      ext_data=0, ext_header=0)
                # divide by median over the region [set_blackbox.flat_norm_sec]
                mean, std, median = clipped_stats(flat_temp[set_blackbox.flat_norm_sec])
                print ('flat name: {}, mean: {}, std: {}, median: {}'.format(flat, mean, std, median))
                flat_cube[i_flat] = flat_temp / median

                if i_flat==0:
                    for key in header_temp.keys():
                        if 'BIASM' in key or 'RDN' in key:
                            del header_temp[key]
                    header_mflat = header_temp

                flat_short = flat.split('/')[-1]
                header_mflat['FLAT{}'.format(i_flat+1)] = (
                    flat_short, 'name reduced flat {}'.format(i_flat+1))
                if 'ORIGFILE' in header_temp.keys():
                    flat_orig = header_temp['ORIGFILE']
                    header_mflat['FLATOR{}'.format(i_flat+1)] = (
                        flat_orig, 'name original flat {}'.format(i_flat+1))

            
            # determine the clipped mean
            #flat_mean, flat_median, flat_std = sigma_clipped_stats(flat_cube, axis=0)
            # or simply the median:
            flat_median = np.median(flat_cube, axis=0)

            # add some header keywords to the master flat
            sec_temp = set_blackbox.flat_norm_sec
            value_temp = '[{}:{},{}:{}]'.format(sec_temp[0].start+1, sec_temp[0].stop+1,
                                                sec_temp[1].start+1, sec_temp[1].stop+1) 
            header_mflat['STATSEC'] = (value_temp, 'pre-defined statistics section [y1:y2,x1:x2]')
            header_mflat['SECMED'] = (np.median(flat_median[sec_temp]), '[e-] median master flat over STATSEC')
            header_mflat['SECSTD'] = (np.std(flat_median[sec_temp]),    '[e-] sigma (STD) master flat over STATSEC')

            # for full image statistics, discard masked pixels
            mask_ok = (data_mask==0)
            header_mflat['FLATMED'] = (np.median(flat_median[mask_ok]), '[e-] median master flat')
            header_mflat['FLATSTD'] = (np.std(flat_median[mask_ok]), '[e-] sigma (STD) master flat')
                
            # write to output file
            fits.writeto(fits_mflat, flat_median.astype('float32'), header_mflat,
                         overwrite=True)


    log.info('reading master flat')
    flat_median = read_hdulist(fits_mflat, ext_data=0)
    if os.path.islink(fits_mflat):
        mflat_name = os.readlink(fits_mflat)
    else:
        mflat_name = fits_mflat
    header['MFLAT-F'] = (mflat_name.split('/')[-1], 'name of master flat applied')
       
    # divide data by the normalised flat
    # do not consider pixels with zero values or edge pixels
    mask_ok = ((flat_median != 0) & (data_mask != set_zogy.mask_value['edge']))
    data[mask_ok] /= flat_median[mask_ok]
               
    if set_zogy.timing:
        log_timing_memory (t0=t, label='mflat_corr', log=log)

    return data
    

################################################################################

def mbias_corr(data, header, bias_path, date_eve):

    if set_zogy.timing:
        t = time.time()
        
    fits_mbias = '{}/bias_{}.fits'.format(bias_path, date_eve)
    
    if not os.path.isfile(unzip(fits_mbias)):

        # prepare master bias from biases in [bias_path]
        bias_list = sorted(glob.glob(bias_path+'/*fits*'))

        # initialize cube of biases to be combined
        nbias = np.shape(bias_list)[0]
        
        # if there are too few bias frames to make tonight's master
        # bias, look for a nearby master bias instead
        if nbias < 5:
            
            fits_mbias_close = get_closest_biasflat(date_eve, 'bias')

            if fits_mbias_close is not None:

                fits_mbias_close = unzip(fits_mbias_close)
                print ('Warning: too few biases available to produce master bias; instead using\n{}'
                       .format(fits_mbias_close))            
                # create symbolic link so future files will automatically
                # use this as the master bias
                os.symlink(fits_mbias_close, fits_mbias)
                
            else:
                log.error('Error: no alternative master bias found')
                return data
            
        else:
            
            print ('making master bias')

            # assuming that biases have the same shape as the input data
            ysize, xsize = np.shape(data)
            bias_cube = np.zeros((nbias, ysize, xsize), dtype='float32')

            # fill the cube
            for i_bias, bias in enumerate(bias_list):
                bias_cube[i_bias], header_temp = read_hdulist(bias_list[i_bias],
                                                              ext_data=0, ext_header=0)
                if i_bias==0:
                    for key in header_temp.keys():
                        if 'BIASM' in key or 'RDN' in key:
                            del header_temp[key]
                    header_mbias = header_temp
                
                bias_short = bias.split('/')[-1]
                header_mbias['BIAS{}'.format(i_bias+1)] = (
                    bias_short, 'name gain/os-corrected bias frame {}'.format(i_bias+1))
                if 'ORIGFILE' in header_temp.keys():
                    bias_orig = header_temp['ORIGFILE']
                    header_mbias['BIASOR{}'.format(i_bias+1)] = (
                        bias_orig, 'name original bias frame {}'.format(i_bias+1))

            
            # determine the clipped mean
            #bias_mean, bias_median, bias_std = sigma_clipped_stats(bias_cube, axis=0)
            # or simply the mean:
            bias_median = np.median(bias_cube, axis=0)

            # add some header keywords to the master bias
            mean_mbias, std_mbias = clipped_stats(bias_median, get_median=False)
            header_mbias['BIASMEAN'] = (mean_mbias, '[e-] mean master bias')
            header_mbias['RDNOISE'] = (std_mbias, '[e-] sigma (STD) master bias')

            # including the means and standard deviations of the master
            # bias in the separate channels
            data_sec_red = set_blackbox.data_sec_red
            nchans = np.shape(data_sec_red)[0]
            mean_chan = np.zeros(nchans)
            std_chan = np.zeros(nchans)

            for i_chan in range(nchans):
                data_chan = bias_median[data_sec_red[i_chan]]
                mean_chan[i_chan], std_chan[i_chan] = clipped_stats(data_chan, get_median=False)
            for i_chan in range(nchans):
                header_mbias['BIASM{}'.format(i_chan+1)] = (
                    mean_chan[i_chan], '[e-] channel {} mean master bias'.format(i_chan+1))
            for i_chan in range(nchans):
                header_mbias['RDN{}'.format(i_chan+1)] = (
                    std_chan[i_chan], '[e-] channel {} sigma (STD) master bias'.format(i_chan+1))
        
            # write to output file
            fits.writeto(fits_mbias, bias_median.astype('float32'), header_mbias,
                         overwrite=True)


    log.info('reading master bias')
    bias_median = read_hdulist(fits_mbias, ext_data=0)

    if os.path.islink(fits_mbias):
        mbias_name = os.readlink(fits_mbias)
    else:
        mbias_name = fits_mbias
    header['MBIAS-F'] = (mbias_name.split('/')[-1], 'name of master bias applied')
    
    # subtract from data
    data -= bias_median
               
    if set_zogy.timing:
        log_timing_memory (t0=t, label='mbias_corr', log=log)

    return data
    

################################################################################

def get_closest_biasflat (date_eve, file_type, filt=None):

    search_str = '{}/*/*/*/{}/{}'.format(set_blackbox.red_dir, file_type,
                                         file_type+'_????????')
    if filt is None:
        search_str += '.fits*'
    else:
        search_str += '_{}.fits*'.format(filt)

    files = glob.glob(search_str)
    nfiles = len(files)

    if nfiles > 0:
    
        # find file that is closest in time to [date_eve]
        mjds = np.array([date2mjd(files[i].split('/')[-1][5:13])
                         for i in range(nfiles)])
        i_close = np.argmin(abs(mjds - date2mjd(date_eve)))
        return files[i_close]

    else:
        return None
    

################################################################################

def date2mjd (date_str, get_jd=False, date_format='%Y%m%d'):
    
    """convert [date_str] in format [date_format] to MJD or JD if [get_jd]
       is set"""

    date = dt.datetime.strptime(date_str, date_format)
    jd = int(date.toordinal()) + 1721424.5
    
    if get_jd:
        return jd
    else:
        return jd - 2400000.5
    

################################################################################

def set_header(header, filename):

    keys = header.keys()

    #if 'GPSSTART' in keys and 'GPSEND' in keys and 'EXPTIME' in keys:

    if 'BUNIT' not in keys:
        header['BUNIT'] = ('ADU', 'Physical unit of array values')        
        
    header['ORIGFILE'] = (filename.split('/')[-1], 'ABOT original file name')
    
    return header

    
################################################################################

def os_corr(data, header):

    """Function that corrects [data] for the overscan signal in the
       vertical and horizontal overscan strips. The definitions of the
       different data/overscan/channel sections are taken from
       [set_blackbox].  The function returns a data array that consists of
       the data sections only, i.e. without the overscan regions. The
       [header] is update in plac.

    """
 
    if set_zogy.timing:
        t = time.time()

    chan_sec = set_blackbox.chan_sec
    data_sec = set_blackbox.data_sec
    os_sec_hori = set_blackbox.os_sec_hori
    os_sec_vert = set_blackbox.os_sec_vert
    data_sec_red = set_blackbox.data_sec_red
    
    # PMV 2018/08/01: this is a constant used inside the loop
    dcol = 11 # after testing, 21 seems a decent width to use

    # number of data columns and rows in the channel
    ncols = set_blackbox.dx - set_blackbox.os_xsize
    nrows = set_blackbox.dy - set_blackbox.os_ysize

    # initialize output data array (without overscan sections)
    ysize_out = set_blackbox.ysize - set_blackbox.ny * set_blackbox.os_ysize
    xsize_out = set_blackbox.xsize - set_blackbox.nx * set_blackbox.os_xsize
    data_out = np.zeros((ysize_out, xsize_out), dtype='float32')

    # and arrays to calculate average means and stds over all channels
    nchans = np.shape(data_sec)[0]
    mean_vos = np.zeros(nchans)
    std_vos = np.zeros(nchans)

    for i_chan in range(nchans):

        # first subtract the clipped mean (not median!) of the
        # vertical overcan section from the entire channel
        data_vos = data[os_sec_vert[i_chan]]
        mean_vos[i_chan], std_vos[i_chan] = clipped_stats(data_vos, get_median=False)
        #data[chan_sec[i_chan]] -= mean_vos[i_chan]
                
        # determine the running clipped mean of the overscan using all
        # values across [dcol] columns, for [ncols] columns
        data_hos = data[os_sec_hori[i_chan]]
        mean_hos, median_hos, std_hos = sigma_clipped_stats(data_hos, axis=0)
        oscan = [np.mean(mean_hos[max(k-int(dcol/2.),0):min(k+int(dcol/2.)+1,ncols)])
                 for k in range(ncols)]
        # do not use the running mean for the first column
        oscan[0] = mean_hos[0]
        # subtract horizontal overscan 
        data[data_sec[i_chan]] -= np.vstack([oscan]*nrows)
        # broadcast into [data_out]
        data_out[data_sec_red[i_chan]] = data[data_sec[i_chan]] 


    # add headers outside above loop to make header more readable
    for i_chan in range(nchans):
        header['BIASM{}'.format(i_chan+1)] = (
            mean_vos[i_chan], '[e-] channel {} mean vertical overscan'.format(i_chan+1))
    for i_chan in range(nchans):
        header['RDN{}'.format(i_chan+1)] = (
            std_vos[i_chan], '[e-] channel {} sigma (STD) vertical overscan'.format(i_chan+1))
                
    # write the average from both the means and standard deviations
    # determined for each channel to the header
    header['BIASMEAN'] = (np.mean(mean_vos), '[e-] average all channel means vert. overscan')
    header['RDNOISE'] = (np.mean(std_vos), '[e-] average all channel sigmas vert. overscan')
        
    if set_zogy.timing:
        log_timing_memory (t0=t, label='os_corr', log=log)

    return data_out


################################################################################

def xtalk_corr (data, crosstalk_file):

    # basically the same as Kerry's function
        
    if set_zogy.timing:
        t = time.time()

    victim, source, correction = np.loadtxt(crosstalk_file,unpack=True)
    corrected = []
    #data = data[0]
    height,width = set_blackbox.dy, set_blackbox.dx # = ccd_sec()
    for k in range(len(victim)):
        if victim[k] < 9:
            j, i = 1, 0
        else:
            j, i = 0, 8
        data[height*j:height*(j+1),width*(int(victim[k])-1-i):width*(int(victim[k])-i)] -= data[height*j:height*(j+1),width*(int(source[k])-1-i):width*(int(source[k])-i)]*correction[k]

    if set_zogy.timing:
        log_timing_memory (t0=t, label='xtalk_corr', log=log)
        
    return data

    # N.B.: note that the channel numbering here are not the same as that assumed
    # with the gain:
    # 
    # [ 0, 1,  2,  3,  4,  5,  6,  7]
    # [ 8, 9, 10, 11, 12, 13, 14, 15]
    # 
    # height,width = 5300, 1500 # = ccd_sec()
    # for victim in range(1,17):
    #     if victim < 9:
    #         j, i = 1, 0
    #     else:
    #         j, i = 0, 8
    #     print (victim, height*j, height*(j+1), width*(int(victim)-1-i), width*(int(victim)-i))
    #
    # victim is not the channel index, but number
    #
    # [vpn224246:~] pmv% python test_xtalk.py
    # 1 5300 10600 0 1500
    # 2 5300 10600 1500 3000
    # 3 5300 10600 3000 4500
    # 4 5300 10600 4500 6000
    # 5 5300 10600 6000 7500
    # 6 5300 10600 7500 9000
    # 7 5300 10600 9000 10500
    # 8 5300 10600 10500 12000
    # 9 0 5300 0 1500
    # 10 0 5300 1500 3000
    # 11 0 5300 3000 4500
    # 12 0 5300 4500 6000
    # 13 0 5300 6000 7500
    # 14 0 5300 7500 9000
    # 15 0 5300 9000 10500
    # 16 0 5300 10500 12000

    
################################################################################

def gain_corr(data, header):

    if set_zogy.timing:
        t = time.time()

    """Returns [data] corrected for the [gain] defined in [set_blackbox.gain]
       for the different channels

    """

    gain = set_blackbox.gain
    chan_sec = set_blackbox.chan_sec
    for i_chan in range(np.shape(chan_sec)[0]):
        data[chan_sec[i_chan]] *= gain[i_chan]
        header['GAIN{}'.format(i_chan+1)] = (gain[i_chan], 'gain applied to channel {}'.format(i_chan+1))

    if set_zogy.timing:
        log_timing_memory (t0=t, label='gain_corr', log=log)
        
    return data

    # check if different channels in [set_blackbox.gain] correspond to the
    # correct channels; currently indices of gain correspond to the
    # channels as follows:
    #
    # [ 8, 9, 10, 11, 12, 13, 14, 15]
    # [ 0, 1,  2,  3,  4,  5,  6,  7]

    # g = gain()
    # height,width = 5300, 1500
    # for (j,i) in [(j,i) for j in range(2) for i in range(8)]:
    #     data[height*j:height*(j+1),width*i:width*(i+1)]*=g[i+(j*8)]
    #
    # height, width = 5300, 1500
    # for (j,i) in [(j,i) for j in range(2) for i in range(8)]: print (height*j, height*(j+1),width*i, width*(i+1), i+(j*8))
    # 0 5300 0 1500 0
    # 0 5300 1500 3000 1
    # 0 5300 3000 4500 2
    # 0 5300 4500 6000 3
    # 0 5300 6000 7500 4
    # 0 5300 7500 9000 5
    # 0 5300 9000 10500 6
    # 0 5300 10500 12000 7
    # 5300 10600 0 1500 8
    # 5300 10600 1500 3000 9
    # 5300 10600 3000 4500 10
    # 5300 10600 4500 6000 11
    # 5300 10600 6000 7500 12
    # 5300 10600 7500 9000 13
    # 5300 10600 9000 10500 14
    # 5300 10600 10500 12000 15


################################################################################

def get_path (telescope, date, dir_type):
    
    # define path
    if date is None:
        q.put(logger.critical('no [date] provided; exiting'))
        raise SystemExit
    else:
        # date can be any of yyyy/mm/dd, yyyy.mm.dd, yyyymmdd,
        # yyyy-mm-dd or yyyy-mm-ddThh:mm:ss.s; if the latter is
        # provided, make sure to set [date_dir] to the date of the
        # evening before UT midnight
        if 'T' in date:            
            if '.' in date:
                date_format = '%Y-%m-%dT%H:%M:%S.%f'
                high_noon = 'T12:00:00.0'
            else:
                date_format = '%Y-%m-%dT%H:%M:%S'
                high_noon = 'T12:00:00'

            date_ut = dt.datetime.strptime(date, date_format).replace(tzinfo=gettz('UTC'))
            date_noon = date.split('T')[0]+high_noon
            date_local_noon = dt.datetime.strptime(date_noon, date_format).replace(tzinfo=gettz(set_zogy.obs_timezone))
            if date_ut < date_local_noon: 
                # subtract day from date_only
                date = (date_ut - dt.timedelta(1)).strftime('%Y-%m-%d')
            else:
                date = date_ut.strftime('%Y-%m-%d')

        # this [date_eve] in format yyyymmdd is also returned
        date_eve = ''.join(e for e in date if e.isdigit())
        date_dir = '{}/{}/{}'.format(date_eve[0:4], date_eve[4:6], date_eve[6:8])
        

    if telescope is None:
        tel_dir = ''
    else:
        tel_dir = telescope.upper()
        
    if dir_type == 'read':
        root_dir = set_blackbox.raw_dir
    elif dir_type == 'write':
        root_dir = set_blackbox.red_dir
    else:
        log.error('[dir_type] not one of "read" or "write"')
        
    path = '{}/{}/{}'.format(root_dir, tel_dir, date_dir)
    if '//' in path:
        print ('replacing double slash in path name: {}'.format(path))
        path = path.replace('//','/')
    
    return path, date_eve
    

################################################################################
    
def date_obs_get(header):
    '''Returns image observation date in the correct format.

    Returns the observation date of the image from the header in the correct format for file names.

    :param header: primary header
    :type header: header
    :returns: str -- '(date)_T(time)'
    '''
    date_obs = header['DATE-OBS'] #load date from header
    date_obs_split = re.split('-|:|T',date_obs) #split date into date and time
    return date_obs_split[0]+date_obs_split[1]+date_obs_split[2]+'_'+date_obs_split[3]+date_obs_split[4]+date_obs_split[5]

    
################################################################################

def sort_files(read_path, file_name):

    """Function to sort raw files by type.  Globs all files in read_path
       and to sorts files into bias, flat and science images using the
       IMAGETYP header keyword.  Similar to Kerry's function in
       BGreduce, slightly adapted as sorting by filter is not needed.

    """
       
    all_files = sorted(glob.glob(read_path+'/'+file_name)) #glob all raw files and sort
    bias = [] #list of biases
    flat = [] #list of flats
    science = [] # list of science images
    for i in range(len(all_files)): #loop through raw files

        if '.fz' not in all_files[i]:
            header = read_hdulist(all_files[i], ext_header=0)
        else:
            header = read_hdulist(all_files[i], ext_header=1)

        imgtype = header['IMAGETYP'].lower() #get image type
        
        if 'bias' in imgtype: #add bias files to bias list
            bias.append(all_files[i])
        if 'flat' in imgtype: #add flat files to flat list
            flat.append(all_files[i])
        if 'object' in imgtype: #add science files to science list
            science.append(all_files[i])

    list_temp = [bias, flat, science]
    return [item for sublist in list_temp for item in sublist]


################################################################################

def unzip(imgname, timeout=None):

    """Unzip a gzipped of fpacked file.
       Same [subpipe] function STAP_unzip.
    """

    #lock.acquire()

    if '.gz' in imgname:
        print ('gunzipping {}'.format(imgname))
        subprocess.call(['gunzip',imgname])
        imgname = imgname.replace('.gz','')
    elif '.fz' in imgname:
        print ('funpacking {}'.format(imgname))
        subprocess.call(['funpack','-D',imgname])
        imgname = imgname.replace('.fz','')

    #lock.release()

    return imgname
        

    
################################################################################

class MyLogger(object):
    '''Logger to control logging and uploading to slack.

    :param log: pipeline log file
    :type log: Logger
    :param mode: mode of pipeline
    :type mode: str
    :param log_stream: stream for log file
    :type log_stream: instance
    :param slack_upload: upload to slack
    :type slack_upload: bool
    '''

    def __init__(self, log, mode, log_stream, slack_upload):
        self._log = log
        self._mode = mode
        self._log_stream = log_stream
        self._slack_upload = slack_upload

    def info(self, text):
        '''Function to log at the INFO level.

        Logs messages to log file at the INFO level. If the night mode of the pipeline
        is running and 'Successfully' appears in the message, upload the message to slack.
        This allows only the overall running of the night pipeline to be uploaded to slack.

        :param text: message from pipeline
        :type text: str
        :exceptions: ConnectionError
        '''
        self._log.info(text)
        message = self._log_stream.getvalue()
        #only allow selected messages in night mode of pipeline to upload to slack
        if self._slack_upload is True and self._mode == 'night' and 'Successfully' in message: 
            try:
                self.slack(self._mode,text) #upload to slack
            except ConnectionError: #if connection error occurs, add to log
                self._log.error('Connection error: failed to connect to slack. Above meassage not uploaded.')

    def warn(self, text):
        '''Function to log at the INFO level.

        Logs messages to log file at the WARN level.'''

        self._log.warn(text)
        message = self._log_stream.getvalue()

    def error(self, text):
        '''Function to log at the ERROR level.

        Logs messages to log file at the ERROR level. If the night mode of the pipeline
        is running, upload the message to slack. This allows only the overall running of
        the night pipeline to be uploaded to slack.

        :param text: message from pipeline
        :type text: str
        :exceptions: ConnectionError
        '''
        self._log.error(text)
        message = self._log_stream.getvalue()
        if self._slack_upload is True and self._mode == 'night': #only night mode of pipeline uploads to slack
            try:
                self.slack(self._mode,text) #upload to slack
            except ConnectionError: #if connection error occurs, add to log
                self._log.error('Connection error: failed to connect to slack. Above meassage not uploaded.')

    def critical(self, text):
        '''Function to log at the CRITICAL level.

        Logs messages to log file at the CRITICAL level. If the night mode of the pipeline
        is running, upload the message to slack. This allows only the overall running of
        the night pipeline to be uploaded to slack. Pipeline will exit on critical errror.
        
        :param text: message from pipeline
        :type text: str
        :exceptions: ConnectionError
        :raises: SystemExit
        '''
        self._log.critical(text)
        message = self._log_stream.getvalue()
        if self._slack_upload is True and self._mode == 'night': #only night mode of pipeline uploads to slack
            try:
                self.slack('critical',text) #upload to slack
            except ConnectionError:
                self._log.error('Connection error: failed to connect to slack. Above meassage not uploaded.') #if connection error occurs, add to log
        sys.exit(-1)

    def slack(self, channel, message):
        '''Slack bot for uploading messages to slack.

        :param message: message to upload
        :type message: str
        '''
        slack_client().api_call("chat.postMessage", channel=channel,  text=message, as_user=True)


################################################################################

def copying(file):
    '''Waits for file size to stablize.

    Function that waits until the given file size is no longer changing before returning.
    This ensures the file has finished copying before the file is accessed.

    :param file: file
    :type file: str
    '''
    copying_file = True #file is copying
    size_earlier = -1 #set inital size of file
    while copying_file:
        size_now = os.path.getsize(file) #get current size of file
        if size_now == size_earlier: #if the size of the file has not changed, return
            time.sleep(1)
            return
        else: #if the size of the file has changed
            size_earlier = os.path.getsize(file) #get new size of file
            time.sleep(1) #wait


################################################################################

def action(item_list):
    '''Action to take during night mode of pipeline.

    For new events, continues if it is a file. '''

    print ('event!') #DP: added brackets
    
    #get parameters for list
    event, telescope, mode, read_path = item_list.get(True)
    
    while True:
        try:
            filename = str(event.src_path) #get name of new file
            if 'fits' in filename: #only continue if event is a fits file
                if '_mask' or '_red' not in filename:
                    copying(filename) #check to see if write is finished writing
                    q.put(logger.info('Found new file '+filename))
        except AttributeError: #if event is a file
            filename = event
            q.put(logger.info('Found old file '+filename))
            
        blackbox_reduce (filename, telescope, mode, read_path)


################################################################################

class FileWatcher(FileSystemEventHandler, object):
    '''Monitors directory for new files.

    :param queue: multiprocessing queue for new files
    :type queue: multiprocessing.Queue'''
    
    def __init__(self, queue, telescope, mode, read_path):
        self._queue = queue
        self._telescope = telescope
        self._mode = mode
        self._read_path = read_path
        
    def on_created(self, event):
        '''Action to take for new files.

        :param event: new event found
        :type event: event'''
        self._queue.put([event, self._telescope, self._mode, self._read_path])

        
################################################################################

if __name__ == "__main__":
    
    params = argparse.ArgumentParser(description='User parameters')
    params.add_argument('--telescope', type=str, default='ML1', help='Telescope name')
    params.add_argument('--mode', type=str, default='day', help='Day or night mode of pipeline')
    params.add_argument('--date', type=str, default=None, help='Date to process (yyyymmdd, yyyy-mm-dd, yyyy/mm/dd or yyyy.mm.dd)')
    params.add_argument('--read_path', type=str, default=None, help='Full path to the input raw data directory; if not defined it is determined from [set_blackbox.raw_dir], [telescope] and [date]')
    params.add_argument('--slack', default=True, help='Upload messages for night mode to slack.')
    args = params.parse_args()

    run_blackbox (telescope=args.telescope, mode=args.mode, date=args.date, read_path=args.read_path, slack=args.slack)


