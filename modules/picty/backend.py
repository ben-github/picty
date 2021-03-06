'''

    picty
    Copyright (C) 2013  Damien Moore

License:

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

##standard imports
import cPickle
import gobject
import gtk
from PIL import Image
from PIL import ImageFile
import threading
import os
import sys
import time
import metadata
import datetime
import bisect

##picty imports
import settings
import baseobjects
import viewsupport
import imagemanip
import pluginmanager
from fstools import io
from logger import log

def idle_add(*args):
    gobject.idle_add(*args)
    time.sleep(0.005)


##TODO: ALL JOBS THE TOUCH A VIEW SHOULD BE PASSED THAT VIEW DURING CONSTRUCTION (do not use collection.get_active_view())

class WorkerJob:
    '''
    Base class for jobs performed on the browser's worker thread. Because all jobs share a single thread
    they are run sequentially with higher priority jobs running first. A running job must regularly check for higher priority
    job(s) being added to the queue and if so, pause operation and return, resuming when the higher priority job(s)
    complete
    Jobs:
       * must have an init constructor def __init__(self,worker,collection,browser,*extra_args)
       * should call the base __init__ method in their __init__ constructor providing a name (string) and priority (int/float)
            * this will add name, priority, worker, collection and browser memebers
       * must provide a __call__ method, which is called to start or continue the job
       * can provide a cancel constructor def cancel(self,shutdown) which may be called from the worker
         thread when the job does not have priority but has been cancelled due to shutdown or for some other reason.
       * jobs on this thread need to frequently check that they are the highest in the queue by calling
         worker.jobs.get_highest() (which returns the job with the highest priority)
       * if the job is unfinished it should promptly return False (unless cancelled) - the job will be resumed
         when no higher priority tasks are running
       * when a job is complete or cancelled it should return True signalling that it should be removed from the queue
    The helper function Worker.queue_job(job_class,*args) can be used to instantiate and queue a job and passing
    in default worker, collection, browser arguments (plus optional arguments in args)
    '''
    def __init__(self,name='',priority=50,worker=None,collection=None,browser=None):
        self.state=False
        self.name=name
        self.priority=priority
        self.worker=worker
        self.collection=collection
        self.browser=browser

    def cancel(self,shutdown=False):
        '''
        this job is being cancelled externally
        this method will be called on outstanding jobs giving
        them an opportunity to notify gui etc
        '''
        idle_add(self.browser.update_backstatus,False,'Nothing to do')
        idle_add(self.browser.update_status,2.0,'Nothing to do')

    def __call__(self):
        'subclasses should override this'
        return True


class WorkerJobQueue:
    '''
    maintains the queue of jobs run on the Worker thread
    manipulation of the queue using the member functions should be thread safe
    '''
    def __init__(self):
        self.queue=[]
        self.priority_collection=None
        self.removed_jobs=[]
        self.lock=threading.Lock()

    def ishighestpriority(self,job):
        self.lock.acquire()
        for j in self.queue:
            if j==job:
                self.lock.release()
                return True
            break
        self.lock.release()
        return False

    def gethighest(self):
        self.lock.acquire()
        for j in self.queue:
            self.lock.release()
            return j
        self.lock.release()
        return None

    def set_priority_collection(self,collection):
        self.lock.acquire()
        self.priority_collection=collection
        jsort=[(-1*(j.collection==collection),-j.priority,j) for j in self.queue]
        self.queue=[j[2] for j in sorted(jsort)]
        self.lock.release()

    def get_priority_colleciton(self,collection):
        return self.priority_colelction

    def get_removed_jobs(self):
        '''
        retrieves the list of removed jobs, clearing out the list in the process
        '''
        self.lock.acquire()
        jobs=self.removed_jobs
        self.removed_jobs=[]
        self.lock.release()
        return jobs[:]

#    def find_by_job_class(self,job_class):
#        self.lock.acquire()
#        return [j for j in self.queue if isinstance(j,job_class)]
#        self.lock.release()
#
#    def find_by_job_name(self,job_name):
#        self.lock.acquire()
#        return [j for j in self.queue if j.name==job_name]
#        self.lock.release()
#
#    def find_by_colleciton(self,collection):
#        self.lock.acquire()
#        return [j for j in self.queue if j.collection==collection]
#        self.lock.release()
#
    def has_job(self,job_class=None,collection=None):
        match=self.queue
        if job_class!=None:
            match=[j for j in match if isinstance(j,job_class)]
        if collection!=None:
            match=[j for j in match if j.collection==collection ]
        return len(match)>0

    def clear(self,job_class=None,collection=None,excluded_job=None):
        '''
        removes jobs from the job queue
        job_class, only jobs that are instances of the job_class, all classes if None
        collection, only jobs associated with the collection, jobs associated with all if None
        excluded_job, a specific job that should be excluded from the removal operation
        '''
        self.lock.acquire()
        self.removers=[j for j in self.queue if ##todo: verify that this actually works
            (job_class==None or isinstance(j,job_class)) and
            (collection==None or j.collection==collection) and
            (excluded_job==None or j!=excluded_job) ]
        self.removed_jobs+=self.removers
        self.queue=[j for j in self.queue if not j in self.removers]
        self.lock.release()

    def pop(self,job):
        self.lock.acquire()
        try:
            ind=self.queue.index(job)
        except ValueError:
            self.lock.release()
            return None
        j=self.queue.pop(ind)
        self.lock.release()
        return j

    def add(self,job):
        '''
        add job to the queue. if the job is associated with the "priority_collection"
        then it receives higher priority than jobs not associated with the priority_collection
        otherwise job ordering is determined by the "priority" member of the job.
        '''
        self.lock.acquire()
        for j in xrange(len(self.queue)):
            if (job.collection==self.priority_collection)>=(self.queue[j].collection==self.priority_collection):
                if self.queue[j].priority<job.priority:
                    self.queue.insert(j,job)
                    self.lock.release()
                    return
        self.queue.append(job)
        self.lock.release()

class QuitJob(WorkerJob):
    def __init__(self,worker,collection,browser):
        WorkerJob.__init__(self,'QUIT',1000,worker,collection,browser)


class ThumbnailJob(WorkerJob):
    def __init__(self,worker,collection,browser,queue_onscreen):
        WorkerJob.__init__(self,'THUMBNAIL',950,worker,collection,browser)
        self.queue_onscreen=queue_onscreen
        self.cu_job_queue=[]

    def __call__(self):
        jobs=self.worker.jobs
        i=0
        self.worker.jobs.clear(CollectionUpdateJob,self.collection)
        while jobs.ishighestpriority(self) and len(self.queue_onscreen)>0:
            item=self.queue_onscreen.pop(0)
            if item.thumb!=False and item.thumb is not None:
                continue
            if not self.collection.load_thumbnail(item,False):
                if item.thumb!=False and not self.collection.has_thumbnail(item):
                    self.cu_job_queue.append(item)
                    continue
            i+=1
            if i%20==0:
                idle_add(self.browser.redraw_view,self.collection)
        if len(self.queue_onscreen)==0:
            if i>0:
                idle_add(self.browser.redraw_view,self.collection)
            if len(self.cu_job_queue)>0:
                self.worker.queue_job_instance(CollectionUpdateJob(self.worker,self.collection,self.browser,self.cu_job_queue))
            return True
        return False


class FlexibleJob(WorkerJob):
    def __init__(self, worker, collection, browser, task_cb, complete_cb, limit_to_view = True, *args):
        WorkerJob.__init__(self,'FLEXJOB',900,worker,collection,browser)
        self.task_cb = task_cb
        self.complete_cb = complete_cb
        self.args = args
        self.pos = 0
        self.limit_to_view = limit_to_view

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        continue_cb = lambda: jobs.ishighestpriority(self)

        i=self.pos
        if self.limit_to_view:
            listitems=self.collection.get_active_view()
        else:
            listitems=self.collection
        while i<len(listitems) and jobs.ishighestpriority(self):
            item=listitems(i)
            self.task_cb(self,item,continue_cb,*self.args)
#            idle_add(self.browser.update_status,1.0*i/len(listitems),'Operating on selected images')
            i+=1
        if i<len(listitems) and not self.cancel:
            self.pos=i
        else:
#            idle_add(self.browser.update_status,1.0*i/len(listitems),'Operating on selected images')
#            idle_add(self.browser.resize_and_refresh_view,self.collection)
            self.pos=0
            self.cancel=False
            idle_add(self.complete_cb)
            return True
        return False


class CollectionUpdateJob(WorkerJob):
    def __init__(self,worker,collection,browser,queue):
        WorkerJob.__init__(self,'COLLECTIONUPDATE',900,worker,collection,browser)
        self.queue=queue
        self.view=self.collection.get_active_view()

    def __call__(self):
        jobs=self.worker.jobs
        c=self.collection
        while len(self.queue)>0 and jobs.ishighestpriority(self):
            item=self.queue.pop(0)
            if item.meta==None:
                it=baseobjects.Item(item)
                it.mtime=item.mtime
                ##it.meta=item.meta.copy() ##Already none !!!
                c.load_metadata(item)
                self.browser.lock.acquire()
                if self.view.del_item(it):
                    self.view.add_item(item)
                    idle_add(self.browser.resize_and_refresh_view,self.collection)
                self.browser.lock.release()
            if not c.has_thumbnail(item):
                c.make_thumbnail(item)
                idle_add(self.browser.resize_and_refresh_view,self.collection)
        if len(self.queue)==0:
            return True
        return False


class RecreateThumbJob(WorkerJob):
    def __init__(self,worker,collection,browser,queue):
        WorkerJob.__init__(self,'RECREATETHUMB',850,worker,collection,browser)
        self.queue=queue

    def __call__(self):
        jobs=self.worker.jobs
        c=self.collection
        view=self.collection.get_active_view()
        while len(self.queue)>0 and jobs.ishighestpriority(self):
            idle_add(self.browser.update_status,1.0/(1+len(self.queue)),'Recreating thumbnails')
            item=self.queue.pop(0)
            if item.meta==None:
                it=baseobjects.Item(item)
                c.load_metadata(item)
                if item.meta is not None:
                    it.meta=item.meta.copy()
                    self.browser.lock.acquire()
                    if view.del_item(it):
                        view.add_item(item)
                    self.browser.lock.release()
            if item.meta!=None:
                c.make_thumbnail(item,None,True) ##force creation of thumbnail (3rd arg = True)
                #c.load_thumbnail(item)
                idle_add(self.browser.resize_and_refresh_view,self.collection)
        if len(self.queue)==0:
            idle_add(self.browser.update_status,2.0,'Recreating thumbnails done')
            return True
        return False


class ReloadMetadataJob(WorkerJob):
    def __init__(self,worker,collection,browser,queue):
        WorkerJob.__init__(self,'RELOADMETADATA',800,worker,collection,browser)
        self.queue=queue
        self.count=len(queue)

    def __call__(self):
        jobs=self.worker.jobs
        view=self.collection.get_active_view()
        while len(self.queue)>0 and jobs.ishighestpriority(self):
            idle_add(self.browser.update_status,1.0-1.0*len(self.queue)/self.count,'Reloading metadata')
            item=self.queue.pop(0)
            it=baseobjects.Item(item)
            it.meta=item.meta.copy()
            self.collection.load_metadata(item)
            log.info('reloaded metadata for '+item.uid)
            self.browser.lock.acquire()
            if view.del_item(item):
                view.add_item(item)
            self.browser.lock.release()
        if len(self.queue)==0:
            idle_add(self.browser.update_status,2.0,'Reloading metadata')
            return True
        return False


class LoadCollectionJob(WorkerJob):
    def __init__(self,worker,collection,browser,filename=''):
        WorkerJob.__init__(self,'LOADCOLLECTION',890,worker,collection,browser)
        self.collection_file=filename
        self.pos=0

    def __call__(self):
        jobs=self.worker.jobs
        jobs.clear(None,self.collection,self)
        view=self.collection.get_active_view()
        collection=self.collection
        log.info('Loading collection file %s with type %s',self.collection_file,collection.type)
        idle_add(self.browser.update_backstatus,True,'Loading Collection: %s'%(self.collection_file,))
        view.empty()
        pluginmanager.mgr.callback('t_view_emptied',collection,view)
        if collection._open():
            log.info('Collection opened %s, with collection location %s',collection.id,collection.image_dirs[0])
            if os.path.exists(collection.image_dirs[0]):
                collection.online=True
                if self.browser!=None:
                    gobject.idle_add(self.browser.collection_online,self.collection) ##should probably call worker.coll_set method as well?
                if collection.rescan_at_open:
                    self.worker.queue_job_instance(WalkDirectoryJob(self.worker,self.collection,self.browser))
            else:
                collection.online=False
                if self.browser!=None:
                    gobject.idle_add(self.browser.collection_offline,self.collection) ##should probably call worker.coll_set method as well?
            pluginmanager.mgr.callback_collection('t_collection_loaded',collection)
            if not view.loaded:
                self.worker.queue_job_instance(BuildViewJob(self.worker,self.collection,self.browser))
            else:
                pluginmanager.mgr.callback('t_view_updated',collection,view)
            if collection.type=='LOCALDIR' and collection.path_to_open:
                log.info('Received D-Bus open request for',collection.path_to_open)
                item=baseobjects.Item(collection.get_relpath(collection.path_to_open))
                item.mtime=io.get_mtime(collection.get_path(item))
                imagemanip.load_metadata(item,collection)
                collection.add(item)
                idle_add(collection.mainframe.view_image,item)
            idle_add(self.worker.coll_set.collection_opened,collection.id)
            if self.browser!=None:
                #idle_add(self.browser.post_build_view)
                idle_add(self.browser.resize_and_refresh_view,collection)
                idle_add(self.browser.update_backstatus,False,'Successfully Loaded Collection')
            log.info('Loaded collection with '+str(len(collection))+' images')
        else:
            log.error('Load collection failed %s %s',collection.id,collection.type)
        self.collection_file=''
        return True


class SetOnlineStatusJob(WorkerJob):
    def __init__(self,worker,collection,browser,status=True):
        WorkerJob.__init__(self,'SETONLINESTATUS',880,worker,collection,browser)
        self.status=status

    def __call__(self):
        if self.status:
            self.collection.connect()
            gobject.idle_add(self.browser.collection_online,self.collection) ##should probably call worker.coll_set method as well?
        else:
            self.collection.disconnect()
            gobject.idle_add(self.browser.collection_offline,self.collection) ##should probably call worker.coll_set method as well?
        return True


class SaveCollectionJob(WorkerJob): ##TODO: This job features the nasty hack that the "browser" argument is the mainframe, and mainframe must have an update_status member taking 3 args
    def __init__(self,worker,collection,mainframe):
        WorkerJob.__init__(self,'SAVECOLLECTION',775,worker,collection,mainframe)

    def __call__(self):
        mainframe=self.browser
        self.worker.jobs.clear(None,self.collection,self)
        #idle_add(mainframe.update_backstatus,None,True,'Closing Collection '+self.collection.name)
        log.info('Closing and saving collection %s',self.collection.id)
        self.collection.end_monitor() ##todo: should be called in close
        self.collection.close()
        self.collection.online=False
        self.collection.empty(True)
        view = self.collection.get_active_view()
        pluginmanager.mgr.callback('t_view_emptied',self.collection,view)
        idle_add(self.worker.coll_set.collection_closed,self.collection.id)
        #idle_add(mainframe.update_backstatus,None,False,'Closed Collection')
        return True


class WalkDirectoryJob(WorkerJob):
    '''this walks the collection directory adding new items the collection (but not the view)'''
    def __init__(self,worker,collection,browser):
        WorkerJob.__init__(self,'WALKDIRECTORY',700,worker,collection,browser)
        self.collection_walker=None
        self.notify_items=[]
        self.done=False
        self.last_walk_state=None

    def __call__(self):
        collection=self.collection
        jobs=self.worker.jobs
        self.last_update_time=time.time()
        try:
            if not self.collection_walker:
                log.info('Starting directory walk on %s',collection.image_dirs[0])
                scan_dir=self.collection.image_dirs[0]
                self.collection_walker=os.walk(scan_dir)
                self.done=False
                pluginmanager.mgr.suspend_collection_events(self.collection)
        except StopIteration:
            self.notify_items=[]
            self.collection_walker=None
            log.error('Aborted directory walk on %s',collection.image_dirs[0])
            return True
        scan_dir=collection.image_dirs[0]
        while jobs.ishighestpriority(self):
            try:
                if self.last_walk_state is None:
                    root,dirs,files=self.collection_walker.next()
                else:
                    root,dirs,files = self.last_walk_state
                    self.last_walk_state = None
            except StopIteration:
                self.done=True
                break
            i=0
            if collection.recursive:
                while i<len(dirs):
                    if dirs[i].startswith('.'):
                        dirs.pop(i)
                    else:
                        i+=1
            else:
                del dirs[:]

            idle_add(self.browser.update_backstatus,True,'Scanning for new images')
            while jobs.ishighestpriority(self) and len(files)>0:
                p=files.pop(0)
                r=p.rfind('.')
                if r<=0:
                    continue
                fullpath=os.path.join(root, p)
                relpath=os.path.relpath(os.path.join(root, p),scan_dir)
                mimetype=io.get_mime_type(fullpath)
                if not mimetype.lower().startswith('image') and not mimetype.lower().startswith('video'):
##                    log.debug('Directory walk found invalid mimetype '+mimetype+' for '+fullpath)
                    continue
                mtime=io.get_mtime(fullpath)
                st=os.stat(fullpath)
                if os.path.isdir(fullpath):
##                    log.warning('Directory Walk: item '+fullpath+' is a directory')
                    continue
                item=baseobjects.Item(relpath)
                item.mtime=mtime
                if collection.find(item)<0:
                    if not collection.verify_after_walk:
                        if collection.load_meta:
                            collection.load_metadata(item,notify_plugins=False)
                        elif collection.load_preview_icons:
                            collection.load_thumbnail(item)
                            if not item.thumb:
                                item.thumb=False
                        self.browser.lock.acquire()
                        collection.add(item)
                        self.browser.lock.release()
                        idle_add(self.browser.resize_and_refresh_view,self.collection)
                    else:
                        self.notify_items.append(item)
            # once we have found enough items add to collection and notify browser
            if time.time()>self.last_update_time+1.0 or len(self.notify_items)>100:
                self.last_update_time=time.time()
                self.browser.lock.acquire()
                for item in self.notify_items:
                    collection.add(item,False)
                self.browser.lock.release()
                idle_add(self.browser.resize_and_refresh_view,self.collection)
                self.notify_items=[]
        if self.done:
            log.info('Directory walk complete for '+collection.image_dirs[0])
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            idle_add(self.browser.update_backstatus,False,'Search complete')
            if self.notify_items:
                self.browser.lock.acquire()
                for item in self.notify_items:
                    collection.add(item)
                self.browser.lock.release()
                idle_add(self.browser.resize_and_refresh_view,self.collection)
            self.notify_items=[]
            self.collection_walker=None
            self.done=False
            pluginmanager.mgr.resume_collection_events(self.collection)
            if collection.verify_after_walk:
                self.worker.queue_job_instance(VerifyImagesJob(self.worker,self.collection,self.browser))
            self.last_walk_state=None
            return True
        self.last_walk_state=(root,dirs,files)
        return False


class WalkSubDirectoryJob(WorkerJob):
    '''this walks a sub-folder in the collection directory adding new items to both view and collection'''
    def __init__(self,worker,collection,browser,sub_dir):
        WorkerJob.__init__(self,'WALKSUBDIRECTORY',650,worker,collection,browser)
        self.collection_walker=None
        self.notify_items=[]
        self.done=False
        self.sub_dir=sub_dir
        self.last_walk_state=None

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        view=collection.get_active_view()
        self.last_update_time=time.time()
        try:
            if not self.collection_walker:
                log.info('Starting directory walk on %s',self.sub_dir)
                scan_dir=self.sub_dir
                self.collection_walker=os.walk(scan_dir)
                pluginmanager.mgr.suspend_collection_events(self.collection)
        except StopIteration:
            log.error('Aborted directory walk on %s',self.sub_dir)
            self.notify_items=[]
            self.collection_walker=None
            return True
        while jobs.ishighestpriority(self):
            try:
                if self.last_walk_state is None:
                    root,dirs,files=self.collection_walker.next()
                else:
                    root,dirs,files = self.last_walk_state
                    self.last_walk_state = None
            except StopIteration:
                self.done=True
                break
            i=0
            while i<len(dirs):
                if dirs[i].startswith('.'):
                    dirs.pop(i)
                else:
                    i+=1
            idle_add(self.browser.update_backstatus,True,'Scanning for new images')
            while jobs.ishighestpriority(self) and len(files)>0:
                p=files.pop(0)
                r=p.rfind('.')
                if r<=0:
                    continue
                fullpath=os.path.join(root, p)
                relpath=os.path.relpath(os.path.join(root, p),scan_dir)
                mimetype=io.get_mime_type(fullpath)
                if not mimetype.lower().startswith('image') and not mimetype.lower().startswith('video'):
                    log.debug('Directory walk found invalid mimetype '+mimetype+' for '+fullpath)
                    continue
                mtime=io.get_mtime(fullpath)
                st=os.stat(fullpath)
                if os.path.isdir(fullpath):
                    log.warning('Directory Walk: item '+fullpath+' is a directory')
                    continue
                item=baseobjects.Item(relpath,mtime)
                if collection.find(item)<0:
                    collection.load_metadata(item,notify_plugins=False) ##todo: check if exists already
                    self.browser.lock.acquire()
                    collection.add(item)
                    self.browser.lock.release()
                    idle_add(self.browser.resize_and_refresh_view,self.collection)
        if self.done:
            log.info('Directory walk complete for %s',self.sub_dir)
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            idle_add(self.browser.update_backstatus,False,'Search complete')
            if self.notify_items:
                self.browser.lock.acquire()
                for item in self.notify_items:
                    collection.add(item)
                self.browser.lock.release()
                idle_add(self.browser.resize_and_refresh_view,self.collection)
            self.notify_items=[]
            self.collection_walker=None
            pluginmanager.mgr.resume_collection_events(self.collection)
            self.last_walk_state=None
            return True
        self.last_walk_state=(root,dirs,files)
        return False


def parse_filter_text(text):
    ch_loc=0
    filters=[]
    while 0<=ch_loc<len(text):
        i=text[ch_loc:].find(' ')
        j=text[ch_loc:].find('"')
        if i<j:
            filters.append(text[ch_loc:i])
            ch_loc=i+1
        else:
            k=ch_loc
            ch_loc=text[ch_loc:].find('"')
            if ch_loc>=0:
                ch_loc+=1
                filters.append(text[k:ch_loc])
    return filters


class BuildViewJob(WorkerJob):
    def __init__(self,worker,collection,browser,sort_key=None,filter_text=''):
        WorkerJob.__init__(self,'BUILDVIEW',925,worker,collection,browser)
        self.sort_key=sort_key
        self.pos=0
        self.filter_text=filter_text
        self.superset=None

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        view=self.collection.get_active_view()
        i=self.pos
        self.browser.lock.acquire()
        if i==0:
            log.info('Building view for %s with sort key %s and filter %s',collection.id,self.sort_key,self.filter_text)
            if self.sort_key:
                view.sort_key_text=self.sort_key
            if view.sort_key_text:
                view.key_cb=self.collection.browser_sort_keys[view.sort_key_text]
            view.filters=None
            filter_text=self.filter_text.strip()
            if filter_text.startswith('lastview&'):
                filter_text=filter_text[9:]
                self.superset=view.copy()
            else:
                self.superset=collection
            if filter_text.strip():
                view.set_filter(filter_text)
            else:
                view.clear_filter(filter_text)
            view.empty()
            pluginmanager.mgr.callback('t_view_emptied',collection,view)
            pluginmanager.mgr.suspend_collection_events(collection)
            idle_add(self.browser.update_view)
        lastrefresh=i
        self.browser.lock.release()
        while i<len(self.superset) and jobs.ishighestpriority(self):
            item=self.superset(i)
            if item.meta!=None:
                self.browser.lock.acquire()
                view.add_item(item)
                self.browser.lock.release()
                if i-lastrefresh>1000:
                    lastrefresh=i
                    idle_add(self.browser.resize_and_refresh_view,collection)
                    idle_add(self.browser.update_status,1.0*i/len(self.superset),'Rebuilding image view - %i of %i'%(i,len(self.superset)))
            i+=1
        if i<len(self.superset):  ## and jobs.ishighestpriority(self)
            self.pos=i
            return False
        else:
            self.pos=0
            idle_add(self.browser.resize_and_refresh_view,collection)
            idle_add(self.browser.update_status,2,'View rebuild complete')
            idle_add(self.browser.post_build_view)
            pluginmanager.mgr.resume_collection_events(collection)
            log.info('Rebuild view complete for %s',collection.id)
            return True


class MapImagesJob(WorkerJob):
    def __init__(self,worker,collection,browser,region,callback,limit_to_view=True):
        WorkerJob.__init__(self,'MAPIMAGES',780,worker,collection,browser)
        self.pos=0
        self.cancel=False
        self.limit_to_view=limit_to_view
        self.update_callback=callback
        self.pblist=[]
        self.region=region
        self.max_images=50
        self.im_count=0
        self.restart=False
        self.view=collection.get_active_view()

    def __call__(self):
        jobs=self.worker.jobs
        i=self.pos
        if self.limit_to_view:
            listitems=self.view
        else:
            listitems=collection
        while i<len(listitems) and jobs.ishighestpriority(self) and self.im_count<self.max_images and not self.cancel:
            item=listitems(i)
            if imagemanip.item_in_region(item,*self.region):
                self.collection.load_thumbnail(item)
                if item.thumb:
                    log.debug('Map plugin: found item %s with coordinates on current map view',item)
                    pb=imagemanip.scale_pixbuf(item.thumb,40)
                    self.pblist.append((item,pb))
                    self.im_count+=1
            if self.update_callback and i%100==0:
                idle_add(self.update_callback,self.pblist)
                self.pblist=[]
            i+=1
        if i<len(listitems) and self.im_count<self.max_images and not self.cancel:
            self.pos=i
        else:
            idle_add(self.update_callback,self.pblist)
            self.pblist=[]
            self.pos=0
            self.cancel=False
            if not self.restart:
                return True
            self.restart=False
            self.im_count=0



class RotateThumbJob(WorkerJob):
    def __init__(self,worker,collection,browser,left=True,limit_to_view=True):
        WorkerJob.__init__(self,'ROTATETHUMBS',830,worker,collection,browser)
        self.pos=0
        self.cancel=False
        self.limit_to_view=limit_to_view
        self.left=left
        self.view=self.collection.get_active_view()
        self.rotate_count=0

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        i=self.pos
        if self.limit_to_view:
            listitems=self.view
        else:
            listitems=self.collection
        while i<len(listitems) and jobs.ishighestpriority(self) and not self.cancel:
            item=listitems(i)
            if item.selected:
                rotated=True
                if self.left:
                    imagemanip.rotate_left(item,self.collection)
                else:
                    imagemanip.rotate_right(item,self.collection)
                idle_add(self.browser.update_status,1.0*i/len(listitems),'Rotating selected images')
            i+=1
        if i<len(listitems) and not self.cancel:
            self.pos=i
        else:
            idle_add(self.browser.update_status,1.0*i/len(listitems),'Rotating selected images')
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            self.pos=0
            self.cancel=False
            return True
        return False

SELECT=0
DESELECT=1
INVERT_SELECT=2

class SelectionJob(WorkerJob):
    def __init__(self,worker,collection,browser,mode=SELECT,limit_to_view=True):
        WorkerJob.__init__(self,'SELECTION',825,worker,collection,browser)
        self.pos=0
        self.cancel=False
        self.limit_to_view=limit_to_view
        self.mode=mode
        self.view=self.collection.get_active_view()

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        i=self.pos
        select=self.mode==SELECT
        if self.limit_to_view:
            listitems=self.view
        else:
            listitems=self.collection
        while i<len(listitems) and jobs.ishighestpriority(self) and not self.cancel:
            item=listitems(i)
            prev=item.selected
            if self.mode==INVERT_SELECT:
                item.selected=not prev
            else:
                item.selected=select
            collection.numselected+=item.selected-prev
            if i%100==0:
                idle_add(self.browser.update_status,1.0*i/len(listitems),'Selecting images - %i of %i'%(i,len(listitems)))
            i+=1
        if i<len(listitems) and not self.cancel:
            self.pos=i
        else:
            idle_add(self.browser.update_status,1.0*i/len(listitems),'Selecting images - %i of %i'%(i,len(listitems)))
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            self.pos=0
            self.cancel=False
            return True
        return False

ADD_KEYWORDS=1
REMOVE_KEYWORDS=2
TOGGLE_KEYWORDS=3
RENAME_KEYWORDS=4
CHANGE_META=5

EDIT_COLLECTION=1
EDIT_VIEW=2
EDIT_SELECTION=3

class EditMetaDataJob(WorkerJob):
    def __init__(self,worker,collection,browser,mode,meta,keyword_string='',scope=EDIT_SELECTION):
        WorkerJob.__init__(self,'EDITMETADATA',750,worker,collection,browser)
        self.pos=-1
        self.cancel=False
        self.mode=mode
        self.scope=scope
        self.keyword_string=keyword_string
        self.meta=meta
##PICKLED DICT
#        self.meta=imageinfo.PickledDict(meta)

    def __call__(self):
        collection=self.collection
        view=collection.get_active_view()
        jobs=self.worker.jobs
        if self.pos<0:
            self.pos=0
            pluginmanager.mgr.suspend_collection_events(self.collection)
        i=self.pos
        items=collection if self.scope==EDIT_COLLECTION else view
        if self.mode==ADD_KEYWORDS:
            tags=metadata.tag_split(self.keyword_string)
            tags_lower=[t.lower() for t in tags]
            while i<len(items) and jobs.ishighestpriority(self) and not self.cancel:
                item=items(i)
                if (self.scope!=EDIT_SELECTION or item.selected) and item.meta!=None and item.meta!=False:
                    meta=item.meta.copy()
                    try:
                        tags_kw=meta['Keywords']
                    except:
                        tags_kw=[]
                    tags_kw_lower=[t.lower() for t in tags_kw]
                    new_tags=list(tags_kw)
                    for j in range(len(tags)):
                        if tags_lower[j] not in tags_kw_lower:
                            new_tags.append(tags[j])
                    if len(new_tags)==0:
                        del meta['Keywords']
                    else:
                        meta['Keywords']=new_tags
                    item.set_meta(meta,collection)
                if i%100==0:
                    idle_add(self.browser.update_status,1.0*i/len(items),'Selecting images - %i of %i'%(i,len(items)))
                i+=1
        if self.mode==RENAME_KEYWORDS:
            tags=metadata.tag_split(self.keyword_string) ##tags should contain a pair of keywords (find, replace)
            find_tag=tags[0].lower()
            repl_tag=tags[1] ##todo: can get weird results/errors if tags contains bad data
            while i<len(items) and jobs.ishighestpriority(self) and not self.cancel:
                item=items(i)
                if (self.scope!=EDIT_SELECTION or item.selected) and item.meta!=None and item.meta!=False:
                    meta=item.meta.copy()
                    try:
                        tags_kw=meta['Keywords']
                    except:
                        tags_kw=[]
                    tags_kw_lower=[t.lower() for t in tags_kw]
                    new_tags=list(tags_kw)
                    for j in range(len(tags_kw_lower)):
                        if find_tag==tags_kw_lower[j]:
                            new_tags[j]=repl_tag
                    if new_tags!=list(tags_kw):
                        if len(new_tags)==0 and 'Keywords' in meta:
                            del meta['Keywords']
                        else:
                            meta['Keywords']=new_tags
                        item.set_meta(meta,collection)
                if i%100==0:
                    idle_add(self.browser.update_status,1.0*i/len(items),'Selecting images - %i of %i'%(i,len(items)))
                i+=1
        if self.mode==TOGGLE_KEYWORDS:
            tags=metadata.tag_split(self.keyword_string)
            while i<len(items) and jobs.ishighestpriority(self) and not self.cancel:
                item=items(i)
                if (self.scope!=EDIT_SELECTION or item.selected) and item.meta!=None:
                    imagemanip.toggle_tags(item,tags,collection)
                if i%100==0:
                    idle_add(self.browser.update_status,1.0*i/len(items),'Selecting images - %i of %i'%(i,len(items)))
                i+=1
        if self.mode==REMOVE_KEYWORDS:
            tags=metadata.tag_split(self.keyword_string)
            tags_lower=[t.lower() for t in tags]
            while i<len(items) and jobs.ishighestpriority(self) and not self.cancel:
                item=items(i)
                if (self.scope!=EDIT_SELECTION or item.selected) and item.meta!=None and item.meta!=False:
                    meta=item.meta.copy()
                    try:
                        tags_kw=list(meta['Keywords'])
                        tags_kw_lower=[t.lower() for t in tags_kw]
                        new_tags=[]
                        for j in range(len(tags_kw)):
                            if tags_kw_lower[j] not in tags_lower:
                                new_tags.append(tags_kw[j])
                        if len(new_tags)==0:
                            del meta['Keywords']
                        else:
                            meta['Keywords']=new_tags
                        item.set_meta(meta,collection)
                    except:
                        pass
                if i%100==0:
                    idle_add(self.browser.update_status,1.0*i/len(items),'Removing keywords - %i of %i'%(i,len(items)))
                i+=1

        if self.mode==CHANGE_META:
            while i<len(items) and jobs.ishighestpriority(self) and not self.cancel:
                item=items(i)
                if (self.scope!=EDIT_SELECTION or item.selected) and item.meta!=None and item.meta!=False:
                    for k,v in self.meta.iteritems():
                        item.set_meta_key(k,v,collection)
                if i%100==0:
                    idle_add(self.browser.update_status,1.0*i/len(items),'Setting keywords - %i of %i'%(i,len(items)))
                i+=1

        if i<len(items) and not self.cancel:
            self.pos=i
        else:
            idle_add(self.browser.update_status,2.0,'Metadata edit complete - %i of %i'%(i,len(items)))
            idle_add(self.browser.resize_and_refresh_view,collection)
            self.pos=0
            self.cancel=False
            pluginmanager.mgr.resume_collection_events(collection)
            return True
        return False


class SaveViewJob(WorkerJob):
    def __init__(self,worker,collection,browser,save,selected_only):
        WorkerJob.__init__(self,'SAVEVIEW',750,worker,collection,browser)
        self.pos=0
        self.cancel=False
        self.selected_only=selected_only
        self.save=save

    def __call__(self):
        jobs=self.worker.jobs
        i=self.pos
        listitems=self.collection.get_active_view()
        while i<len(listitems) and jobs.ishighestpriority(self) and not self.cancel:
            inc=True
            item=listitems(i)
            if not self.selected_only or listitems(i).selected:
                if self.save:
                    ##delete the file, or write any metadata changes.
                    if item.is_meta_changed()==2:
                        self.collection.delete_item(item)
                        idle_add(self.browser.resize_and_refresh_view,self.collection)
                        idle_add(self.browser.update_status,1.0*i/len(listitems),'Committing chages in view - %i of %i'%(i,len(listitems)))
                        inc=False
                    elif item.is_meta_changed():
                        self.collection.write_metadata(item)
                        idle_add(self.browser.resize_and_refresh_view,self.collection)
                        idle_add(self.browser.update_status,1.0*i/len(listitems),'Committing changes in view - %i of %i'%(i,len(listitems)))
                else:
                    ##revert the deletion mark and any changes to the image metadata
                    if item.is_meta_changed()==2:
                        item.delete_revert()
                        idle_add(self.browser.resize_and_refresh_view,self.collection)
                        idle_add(self.browser.update_status,1.0*i/len(listitems),'Reverting changes in view - %i of %i'%(i,len(listitems)))
                    if item.is_meta_changed():
                        try:
                            orient=item.meta['Orientation']
                        except:
                            orient=None
                        try:
                            orient_backup=item.meta_backup['Orientation']
                        except:
                            orient_backup=None
                        item.meta_revert(self.collection)
                        ##todo: need to recreate thumb if orientation changed
                        if orient!=orient_backup:
                            item.thumb=None
                            self.worker.queue_job_instance(RecreateThumbJob(self.worker,self.collection,self.browser,[item]))
                        idle_add(self.browser.resize_and_refresh_view,self.collection)
                        idle_add(self.browser.update_status,1.0*i/len(listitems),'Reverting changes in view - %i of %i'%(i,len(listitems)))
            if i%100==0:
                if self.save:
                    idle_add(self.browser.update_status,1.0*i/len(listitems),'Committing changes in view - %i of %i'%(i,len(listitems)))
                else:
                    idle_add(self.browser.update_status,1.0*i/len(listitems),'Reverting changes in view - %i of %i'%(i,len(listitems)))
            if inc:
                i+=1
        if i<len(listitems) and not self.cancel:
            self.pos=i
        else:
            idle_add(self.browser.update_status,2.0,'Saving images complete')
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            self.pos=0
            self.cancel=False
            return True
        return False


class VerifyImagesJob(WorkerJob):
    def __init__(self,worker,collection,browser):
        WorkerJob.__init__(self,'VERIFYIMAGES',500,worker,collection,browser)
        self.countpos=-1
        self.view=self.collection.get_active_view()

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        if self.countpos<0:
            log.info('Starting image verification job')
            self.countpos=0
            pluginmanager.mgr.suspend_collection_events(self.collection)
        i=self.countpos  ##todo: make sure this gets initialized
        while i<len(collection) and jobs.ishighestpriority(self):
            item=collection[i]
            if i%50==0:
                idle_add(self.browser.update_backstatus,True,'Verifying images in collection - %i of %i'%(i,len(collection)))
            if item.meta==False: ##TODO: This is a legacy check -- should remove eventually
                item.meta=None
            if item.meta==None:
                log.debug('Verify job loading metadata %s',item.uid)
                self.browser.lock.acquire()
                collection.delete(item)
                self.browser.lock.release()
                collection.load_metadata(item,notify_plugins=False) ##todo: check if exists already
                self.browser.lock.acquire()
                collection.add(item)
                self.browser.lock.release()
#                idle_add(self.browser.resize_and_refresh_view,self.collection) #TODO: too slow to do this update for every image, but not doing so can cause issues with user clicking on one image but another actually being selected
            if not os.path.exists(collection.get_path(item)) or os.path.isdir(collection.get_path(item)) or collection.get_path(item)!=io.get_true_path(collection.get_path(item)):  ##todo: what if mimetype or size changed?
                log.debug('Verify job delete missing item %s',item.uid)
                self.browser.lock.acquire()
                collection.delete(item)
                self.browser.lock.release()
#                idle_add(self.browser.resize_and_refresh_view,self.collection)
                continue
            mtime=io.get_mtime(collection.get_path(item))
            if mtime!=item.mtime:
                log.debug('Verify job mtime changed %s %s %s',item.uid,item.mtime,mtime)
                self.browser.lock.acquire()
                collection.delete(item)
                self.browser.lock.release()
                item.mtime=mtime
                item.image=None
                item.qview=None
                collection.load_metadata(item,notify_plugins=False)
                item.thumb=None
                item.thumburi=None
                self.browser.lock.acquire()
                collection.add(item)
                self.browser.lock.release()
#                idle_add(self.browser.resize_and_refresh_view,self.collection)
            if i%200 == 0:
                idle_add(self.browser.resize_and_refresh_view,self.collection)
            i+=1
        self.countpos=i
        if i>=len(collection):
            self.countpos=0
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            idle_add(self.browser.update_backstatus,False,'Verification complete')
            log.info('Image verification complete')
            pluginmanager.mgr.resume_collection_events(self.collection)
            if collection.store_thumbnails:
                self.worker.queue_job_instance(MakeThumbsJob(self.worker,self.collection,self.browser))
            return True
        return False


class MakeThumbsJob(WorkerJob):
    def __init__(self,worker,collection,browser):
        WorkerJob.__init__(self,'MAKETHUMBS',300,worker,collection,browser)
        self.countpos=0

    def __call__(self):
        jobs=self.worker.jobs
        collection=self.collection
        i=self.countpos
        while i<len(collection) and jobs.ishighestpriority(self):
            item=collection[i]
            if i%50==0:
                idle_add(self.browser.update_backstatus,True,'Validating and creating missing thumbnails - %i of %i'%(i,len(collection)))
                idle_add(self.browser.resize_and_refresh_view,self.collection)
            if not collection.has_thumbnail(item):
                collection.make_thumbnail(item)
#                idle_add(self.browser.update_backstatus,True,'Validating and creating missing thumbnails - %i of %i'%(i,len(collection)))
            i+=1
        self.countpos=i
        if i>=len(collection):
            self.countpos=0
            idle_add(self.browser.update_backstatus,False,'Thumbnailing complete')
            idle_add(self.browser.resize_and_refresh_view,self.collection)
            return True
        return False


class DirectoryUpdateJob(WorkerJob):
    def __init__(self,worker,collection,browser,action_queue):
        WorkerJob.__init__(self,'DIRECTORYUPDATE',400,worker,collection,browser)
        self.queue=action_queue
        self.started=False

    def __call__(self):
        #todo: make sure job.queue has been initialized
        #todo: acquire and release collection lock
        jobs=self.worker.jobs
        if not self.started:
            self.started=True
            pluginmanager.mgr.suspend_collection_events(self.collection)
        while jobs.ishighestpriority(self) and len(self.queue)>0:
            collection,fullpath,action=self.queue.pop(0)
            relpath=collection.get_relpath(fullpath)
            if action in ('DELETE','MOVED_FROM'):
                if not os.path.exists(fullpath):
                    log.debug('Removiing deleted item %s from collection %s',fullpath,collection.id)
                    self.browser.lock.acquire()
                    collection.delete(relpath)
                    self.browser.lock.release()
                    idle_add(self.browser.resize_and_refresh_view,collection)
            if action in ('MOVED_TO','MODIFY','CREATE'):
                if os.path.exists(fullpath) and os.path.isfile(fullpath):
                    log.debug('Adding new item %s to collection %s',fullpath,collection.id)
                    mimetype=io.get_mime_type(fullpath)
                    if not mimetype.lower().startswith('image') and not mimetype.lower().startswith('video'):
                        continue
                    i=collection.find(relpath)
                    if i>=0:
                        if io.get_mtime(fullpath)!=collection[i].mtime:
                            item=collection[i]
                            self.browser.lock.acquire()
                            collection.delete(item)
                            self.browser.lock.release()
                            item.mtime=io.get_mtime(fullpath)
                            collection.load_metadata(item,notify_plugins=False)
                            collection.make_thumbnail(item) ##todo: queue this onto lower priority job
                            self.browser.lock.acquire()
                            collection.add(item)
                            self.browser.lock.release()
                            idle_add(self.browser.resize_and_refresh_view,self.collection)
                    else:
                        item=baseobjects.Item(relpath)
                        item.mtime=io.get_mtime(fullpath)
                        collection.load_metadata(item,notify_plugins=False)
                        self.browser.lock.acquire()
                        collection.add(item)
                        self.browser.lock.release()
                        if not collection.has_thumbnail(item):
                            collection.make_thumbnail(item) ##todo: queue this onto lower priority job
                        idle_add(self.browser.resize_and_refresh_view,collection)
                if os.path.exists(fullpath) and os.path.isdir(fullpath):
                    if action=='MOVED_TO':
                        log.debug('Adding new directory %s to collection %s',fullpath,collection.id)
                        self.worker.queue_job_instance(WalkSubDirectoryJob(self.worker,collection,self.browser,fullpath))
        if len(self.queue)==0:
            pluginmanager.mgr.resume_collection_events(self.collection)
            return True
        return False


class Worker:
    def __init__(self,coll_set):
        self.coll_set=coll_set
        self.jobs=WorkerJobQueue()
        self.event=threading.Event()
        self.lock=threading.Lock()
        self.thread=threading.Thread(target=self._loop)
        self.dirtimer=None # timer used to delay update after directory change notifications
        self.dirlock=threading.Lock() #lock used to update the queue of deferred directory change notifications
        self.deferred_dir_update_queue=[]
        self.active_collection=None #to be used only on main thread

    def start(self):
        self.thread.start()

    def _loop(self):
        log.info('Worker thread started')
        while 1:
            try:
                rem_jobs=self.jobs.get_removed_jobs()
                if len(rem_jobs)>0:
                    for j in rem_jobs: ##clean up any cancelled jobs
                        j.cancel()
                if self.jobs.gethighest()==None:
                    self.event.clear()
                    self.event.wait()
                job=self.jobs.gethighest()
                if isinstance(job,QuitJob):
                    log.info('Worker thread finished')
                    return
                if job:
                    if job():
                        self.jobs.pop(job)
            except:
                import traceback
                tb_text=traceback.format_exc(sys.exc_info()[2])
                log.error("Error on Worker Thread\n"+tb_text)
                job=self.jobs.gethighest()
                if job:
                    log.info("Abandoning Highest Priority Task "+job.name+" and Resuming Worker Loop")
                    self.jobs.pop(job)

    def set_active_collection(self,collection):
        '''
        the active collection is the default collection passed to the job requests in the methods below
        '''
        self.active_collection=collection
        self.jobs.set_priority_collection(collection)

    def get_active_collection(self):
        return self.active_collection

    def get_default_job_tuple(self):
        return (self,self.active_collection,self.active_collection.browser)

    def queue_job_instance(self,job_instance):
        self.jobs.add(job_instance)
        self.event.set()

    def queue_job(self,job_class,*extra_args):
        self.jobs.add(job_class(self,self.active_collection,self.active_collection.browser,*extra_args))
        self.event.set()

    def kill_jobs_by_class(self,job_class):
        self.jobs.clear_by_job_class(job_class)

    def scan_and_verify(self,collection):
        ##todo: clear out other queued jobs in the scan and verify chain for this collection
        self.queue_job(WalkDirectoryJob,collection)

    def quit(self):
        sj=QuitJob(self,None,None)
        self.queue_job_instance(sj)
        while self.thread.isAlive(): ##todo: replace this with a notification
            time.sleep(0.1)

    def request_map_images(self,region,callback):
        self.queue_job(MapImagesJob,region,callback)

    def request_thumbnails(self,itemlist):
        if not self.jobs.has_job(job_class=ThumbnailJob,collection=self.active_collection):
            self.queue_job(ThumbnailJob,itemlist)

    def rotate_selected_thumbs(self,left=True):
        self.queue_job(RotateThumbJob,left)

    def recreate_thumb(self,item):
        self.queue_job(RecreateThumbJob,[item])

    def recreate_thumb(self,item):
        self.queue_job(RecreateThumbJob,[item])

    def recreate_selected_thumbs(self):
        self.queue_job(RecreateThumbJob,self.active_collection.get_active_view().get_selected_items())

    def reload_metadata(self,item):
        self.queue_job(ReloadMetadataJob,[item])

    def reload_selected_metadata(self):
        self.queue_job(ReloadMetadataJob,self.active_collection.get_active_view().get_selected_items())

    def select_all_items(self,mode=SELECT,view=True):
        self.queue_job(SelectionJob,mode,view)

    def info_edit(self,meta):
        self.queue_job(EditMetaDataJob,CHANGE_META,meta)

    def keyword_edit(self,keyword_string,toggle=False,remove=False,replace=False,scope=EDIT_SELECTION):
        if toggle:
            mode=TOGGLE_KEYWORDS
        elif remove:
            mode=REMOVE_KEYWORDS
        elif replace:
            mode=RENAME_KEYWORDS
        else:
            mode=ADD_KEYWORDS
        self.queue_job(EditMetaDataJob,mode,None,keyword_string,scope)

    def rebuild_view(self,sort_key,filter_text=''):
        self.jobs.clear(BuildViewJob,self.get_active_collection()) ##todo: other jobs probably need to clear out active jobs of the same type (rather than queue)
        self.queue_job(BuildViewJob,sort_key,filter_text)

    def save_or_revert_view(self,save=True,selected_only=False):
        self.queue_job(SaveViewJob,save,selected_only)

    def deferred_dir_update(self):
        self.dirlock.acquire()
        log.debug('Starting deferred directory monitor event handler')
        self.queue_job_instance(DirectoryUpdateJob(self,None,self.active_collection.browser,self.deferred_dir_update_queue[:])) #queue a verify job since we won't get individual image removal notifications
        del self.deferred_dir_update_queue[:]
        self.dirtimer=None
        self.dirlock.release()

    def directory_change_notify(self,collection,path,action,isdir):
        homedir=os.path.normpath(collection.image_dirs[0])
        #ignore notifications on files in a hidden dir or not in the image dir
        if os.path.normpath(os.path.commonprefix([path,homedir]))!=homedir:
            log.debug('Invalid directory change notification: %s %s',path,action)
            return
        ppath=path
        while ppath!=homedir:
            ppath,name=os.path.split(ppath)
            ppath=os.path.normpath(ppath)
            if name.startswith('.') or name=='':
                log.debug('Invalid directory change notification: %s %s',path,action)
                return
        if isdir:
            log.debug('Directory changed notification on path: %s %s',path,action)
            if action in ('MOVED_FROM','DELETE'):
                self.queue_job_instance(VerifyImagesJob(self,collection,self.active_collection.browser)) #queue a verify job since we won't get individual image removal notifications
                return
        #valid file or a moved directory, so queue the update
        #(modify and create notifications are
        #only processed after some delay because many events may be generated
        #before the files are closed)
        ##todo: respond to file close events instead of modify/create events??
        self.dirlock.acquire()
        if self.dirtimer!=None:
            self.dirtimer.cancel()
        self.dirtimer=threading.Timer(3,self.deferred_dir_update)
        self.dirtimer.start()
        self.deferred_dir_update_queue.append((collection,path,action))
        self.dirlock.release()
        log.debug('File change event: %s %s',path,action)
