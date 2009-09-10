import os
from pyinotify import *

wm=WatchManager()

if 'IN_DELETE' in dir():
    mask = IN_DELETE | IN_CREATE |IN_DONT_FOLLOW |IN_MODIFY|IN_MOVED_FROM|IN_MOVED_TO  # watched events
else:
    mask = EventsCodes.IN_DELETE | EventsCodes.IN_CREATE |EventsCodes.IN_DONT_FOLLOW |EventsCodes.IN_MODIFY|EventsCodes.IN_MOVED_FROM|EventsCodes.IN_MOVED_TO  # watched events

class Monitor(ProcessEvent):
    def __init__(self,cb):
        ProcessEvent.__init__(self)
        self.cb=cb
        self.notifier = ThreadedNotifier(wm, self)
        self.wdd=None
        self.notifier.start()
    def process_IN_MODIFY(self, event):
        path=os.path.join(event.path, event.name)
        self.cb(path,'MODIFY',event.is_dir)
#        print "Modify: %s" %  path
    def process_IN_MOVED_FROM(self, event):
        path=os.path.join(event.path, event.name)
        self.cb(path,'MOVED_FROM',event.is_dir)
#        print "Moved out: %s" %  path
#        if os.path.isdir(path):
#            wm.rm_watch(path,mask,rec=True)
    def process_IN_MOVED_TO(self, event):
        path=os.path.join(event.path, event.name)
        self.cb(path,'MOVED_TO',event.is_dir)
#        print "Moved in: %s" %  path
#        if os.path.isdir(path):
#            wm.add_watch(path,mask,rec=True)
    def process_IN_CREATE(self, event):
        path=os.path.join(event.path, event.name)
        self.cb(path,'CREATE',event.is_dir)
#        print "Create: %s" %  path
#        if os.path.isdir(path):
#            wm.add_watch(path,mask,rec=True)
    def process_IN_DELETE(self, event):
        path=os.path.join(event.path, event.name)
        self.cb(path,'DELETE',event.is_dir)
#        print "Remove: %s" %  path
#        if os.path.isdir(path):
#            wm.rm_watch(path,mask,rec=True)
    def process_default(self, event=None):
        pass
    def start(self,dir):
        self.wdd = wm.add_watch(dir, mask, rec=True, auto_add=True)
    def stop(self,dir):
        try:
            if self.wdd and dir in self.wdd:
                wm.rm_watch(self.wdd[dir], rec=True)
            self.notifier.stop() ##todo: should be checking if there are any other watches still active?
        except:
            pass
