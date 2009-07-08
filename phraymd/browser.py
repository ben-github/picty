#!/usr/bin/python2.5

'''

    phraymd
    Copyright (C) 2009  Damien Moore

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


##standard python lib imports
import threading
import os
import os.path
import subprocess
import time
import datetime
import bisect
import gobject

import sys
sys.path.insert(0,'/usr/share') ##private module location on installed version

##non-standard libs
try:
    import gobject
    import gnomevfs ##todo: replace with gio
    import gtk
    gobject.threads_init()
    gtk.gdk.threads_init()
    import gnome.ui
    import pyexiv2  ## not actually used in this module, but better to recognize the problem early
except:
    print 'ERROR: missing modules gobject, gtk, gnome.ui, gnomevfs and pyexiv2'
    import sys
    sys.exit()

## local imports
import settings
import viewer
import backend
import metadatadialogs
import imagemanip
import imageinfo
import fileops
import exif
import register_icons
import tagui
import mapui


class ImageBrowser(gtk.HBox):
    '''
    a widget designed to rapidly display a collection of objects
    from an image cache
    '''
    MODE_NORMAL=1
    MODE_TAG=2
    __gsignals__={
        'activate-item':(gobject.SIGNAL_RUN_LAST,gobject.TYPE_NONE,(gobject.TYPE_INT,gobject.TYPE_PYOBJECT)),
        'context-click-item':(gobject.SIGNAL_RUN_LAST,gobject.TYPE_NONE,(gobject.TYPE_INT,gobject.TYPE_PYOBJECT))
        }
    def __init__(self,hover_cmds=()):
        gtk.HBox.__init__(self)
        self.hover_cmds=hover_cmds
        self.configure_geometry()
        self.lock=threading.Lock()
        self.tm=backend.Worker(self)
        self.neededitem=None

        self.mode=self.MODE_NORMAL

        self.pressed_ind=-1
        self.pressed_item=None
        self.last_selected_ind=-1
        self.last_selected=None
        self.button_press_block=False

        self.focal_item=None

        self.shift_state=False

        self.pixbuf_thumb_fail=gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB,True,8,128,128)
        self.pixbuf_thumb_load=gtk.gdk.Pixbuf(gtk.gdk.COLORSPACE_RGB,True,8,128,128)
        self.pixbuf_thumb_fail.fill(0xC0000080)
        self.pixbuf_thumb_load.fill(0xFFFFFF20)

        self.geo_view_offset=0
        self.offsetx=0
        self.geo_ind_view_first=0
        self.geo_ind_view_last=1
        self.hover_ind=-1

        self.imarea=gtk.DrawingArea()
        self.imarea.set_property("can-focus",True)
        self.imarea.set_size_request(160,160)
        self.scrolladj=gtk.Adjustment()
        self.vscroll=gtk.VScrollbar(self.scrolladj)
        self.vscroll.set_property("has-tooltip",True)
        self.vscroll.connect("query-tooltip",self.scroll_tooltip_query)

        self.vbox=gtk.VBox()
        self.vbox.pack_start(self.imarea)
        self.vbox.show()

        self.pack_start(self.vbox)
        self.pack_start(self.vscroll,False)

        self.imarea.connect("realize",self.realize_signal)
        self.imarea.connect("configure_event",self.configure_signal)
        self.imarea.connect("expose_event",self.expose_signal)
        self.imarea.add_events(gtk.gdk.POINTER_MOTION_MASK)
        self.imarea.connect("leave-notify-event",self.mouse_leave_signal)
        self.imarea.add_events(gtk.gdk.LEAVE_NOTIFY_MASK)
        self.imarea.connect("motion-notify-event",self.mouse_motion_signal)
        self.scrolladj.connect("value-changed",self.scroll_signal)
        self.imarea.add_events(gtk.gdk.SCROLL_MASK)
        self.imarea.connect("scroll-event",self.scroll_signal_pane)
        self.imarea.add_events(gtk.gdk.BUTTON_PRESS_MASK)
        self.imarea.add_events(gtk.gdk.BUTTON_RELEASE_MASK)
        self.imarea.connect("button-press-event",self.button_press)
        self.imarea.connect("button-release-event",self.button_press)

        self.imarea.add_events(gtk.gdk.KEY_PRESS_MASK)
        self.imarea.add_events(gtk.gdk.KEY_RELEASE_MASK)
        self.imarea.connect("key-press-event",self.key_press_signal)
        self.imarea.connect("key-release-event",self.key_press_signal)

        target_list=[('image-filename', gtk.TARGET_SAME_APP, 1)]
        target_list=gtk.target_list_add_uri_targets(target_list,0)
        print 'src target list',target_list
        self.imarea.drag_source_set(gtk.gdk.BUTTON1_MASK,
                  target_list,
                  gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_MOVE |  gtk.gdk.ACTION_COPY)

        target_list=[('tag-tree-row', gtk.TARGET_SAME_APP, 0)]
        target_list=gtk.target_list_add_uri_targets(target_list,0)
        print 'dest target list',target_list
        self.imarea.drag_dest_set(gtk.DEST_DEFAULT_ALL,
                target_list,
                gtk.gdk.ACTION_DEFAULT | gtk.gdk.ACTION_COPY)

        self.imarea.connect("drag-data-get",self.drag_get_signal)
        ##self.imarea.connect("drag-begin", self.drag_begin_signal)
        self.imarea.connect("drag-data-received",self.drag_receive_signal)
        #self.imarea.drag_source_set_icon_stock('browser-drag-icon')

        self.imarea.show()
        self.vscroll.show()
        self.imarea.grab_focus()

    def scroll_tooltip_query(self,widget,x, y, keyboard_mode, tooltip):
#        height=widget.window.get_size()[1]
#        yscroll=y*self.scrolladj.upper/height
#        ind=min(len(self.tm.view),max(0,int(yscroll)/(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count))
#        key=self.sort_order.get_active_text()
#        key_fn=imageinfo.sort_keys_str[key]
#        item=self.tm.view(ind)
#        tooltip.set_text(key+': '+str(key_fn(item)))
        return True


    def update_status(self,progress,message):
        ##todo: raise a custom signal
#        self.status_bar.show()
#        if 1.0>progress>=0.0:
#            self.status_bar.set_fraction(progress)
#        if progress<0.0:
#            self.status_bar.pulse()
#        if progress>=1.0:
#            self.status_bar.hide()
#        self.status_bar.set_text(message)
        pass

    def key_press_signal(self,obj,event):
        if event.type==gtk.gdk.KEY_PRESS:
            if event.keyval==65535: #del key
                fileops.worker.delete(self.tm.view,self.update_status)
            elif event.keyval==65362: #up
                self.vscroll.set_value(self.vscroll.get_value()-self.scrolladj.step_increment)
            elif event.keyval==65364: #dn
                self.vscroll.set_value(self.vscroll.get_value()+self.scrolladj.step_increment)
            elif event.keyval==65365: #pgup
                self.vscroll.set_value(self.vscroll.get_value()-self.scrolladj.page_increment)
            elif event.keyval==65366: #pgdn
                self.vscroll.set_value(self.vscroll.get_value()+self.scrolladj.page_increment)
            elif event.keyval==65360: #home
                self.vscroll.set_value(self.scrolladj.lower)
            elif event.keyval==65367: #end
                self.vscroll.set_value(self.scrolladj.upper)
            elif event.keyval==65505: #shift
                self.redraw_view()
            elif event.keyval==65507: #control
                self.redraw_view()
        if event.type==gtk.gdk.KEY_RELEASE:
            if event.keyval==65505: #shift
                self.redraw_view()
            elif event.keyval==65507: #control
                self.redraw_view()
##        return True

    def resize_browser_pane(self):
        w,h=self.hpane.window.get_size()
        if self.hpane.get_position()<self.geo_thumbwidth+2*self.geo_pad+self.hpane_ext.get_position():
            w,h=self.hpane.window.get_size()
            if w<=self.geo_thumbwidth+2*self.geo_pad+self.hpane_ext.get_position():
                self.hpane.set_position(w/2)
            else:
                self.hpane.set_position(self.geo_thumbwidth+2*self.geo_pad+self.hpane_ext.get_position())


##    def view_image(self,item,fullwindow=False):
##        self.iv.show()
##        self.iv.SetItem(item)
##        self.is_iv_showing=True
##        self.update_geometry(True)
##        self.resize_browser_pane()
##        if self.focal_item!=None:
##            ind=self.item_to_view_index(self.focal_item)
##            self.center_view_offset(ind)
##        self.update_scrollbar()
##        self.update_required_thumbs()
##        self.imarea.window.invalidate_rect((0,0,self.geo_width,self.geo_height),True)
##        self.imarea.grab_focus()
##
##    def hide_image(self):
##        self.iv.hide()
##        self.iv.ImageNormal()
##        self.vbox.show()
##        self.hbox.show()
##        self.toolbar.show()
##        self.hpane_ext.show()
##        self.info_bar.show()
##        self.vscroll.show()
##        self.is_iv_fullscreen=False
##        self.is_iv_showing=False
##        self.imarea.grab_focus()
##
##    def button_press_image_viewer(self,obj,event):
##        if event.button==1 and event.type==gtk.gdk._2BUTTON_PRESS:
##            if self.is_iv_fullscreen:
##                self.iv.ImageNormal()
##                self.vbox.show()
##                self.toolbar.show()
##                self.hpane_ext.show()
##                self.info_bar.show()
##                self.is_iv_fullscreen=False
##                if self.is_fullscreen:
##                    self.window.unfullscreen()
##                    self.is_fullscreen=False
##            else:
##                if not self.is_fullscreen:
##                    self.window.fullscreen()
##                    self.is_fullscreen=True
##                    time.sleep(0.1)
##                self.iv.ImageFullscreen()
##                self.vbox.hide()
##                self.toolbar.hide()
##                self.hpane_ext.hide()
##                self.info_bar.hide()
##                self.is_iv_fullscreen=True
##            self.imarea.grab_focus()

    def get_hover_command(self, ind, x, y):
        offset=ind-self.geo_ind_view_first
        left=(offset%self.geo_horiz_count)*(self.geo_thumbwidth+self.geo_pad)
        left+=self.geo_pad/4
        top=self.geo_ind_view_first*(self.geo_thumbheight+self.geo_pad)/self.geo_horiz_count-int(self.geo_view_offset)
        top+=offset/self.geo_horiz_count*(self.geo_thumbheight+self.geo_pad)
        top+=self.geo_pad/4
        for i in range(len(self.hover_cmds)):
            right=left+self.hover_cmds[i][1].get_width()
            bottom=top+self.hover_cmds[i][1].get_height()
            if left<x<=right and top<y<=bottom:
                return i
            left+=self.hover_cmds[i][1].get_width()+self.geo_pad/4
        return -1


    def edit_custom_mime_apps(self,widget,item):
        pass

    def item_make_thumb(self,widget,item):
        self.tm.recreate_thumb(item)

    def item_reload_metadata(self,widget,item):
        self.tm.reload_metadata(item)

    def mime_open(self,widget,app_cmd,item):
        print 'mime_open',app_cmd,item
        subprocess.Popen(app_cmd+' "'+item.filename+'"',shell=True)

    def custom_mime_open(self,widget,app_cmd_template,item):
        from string import Template
        fullpath=item.filename
        directory=os.path.split(item.filename)[0]
        fullname=os.path.split(item.filename)[1]
        name=os.path.splitext(fullname)[0]
        ext=os.path.splitext(fullname)[1]
        app_cmd=Template(app_cmd_template).substitute(
            {'FULLPATH':fullpath,'DIR':directory,'FULLNAME':fullname,'NAME':name,'EXT':ext})
        print 'mime_open',app_cmd,item
        subprocess.Popen(app_cmd,shell=True)

    def save_item(self,widget,item):
        if item.meta_changed:
            imagemanip.save_metadata(item)

    def revert_item(self,widget,item):
        if not item.meta_changed:
            return
        try:
            orient=item.meta['Orientation']
        except:
            orient=None
        try:
            orient_backup=item.meta_backup['Orientation']
        except:
            orient_backup=None
        item.meta_revert()
        if orient!=orient_backup:
            item.thumb=None
            self.tm.recreate_thumb(item)
        self.redraw_view()

    def launch_item(self,widget,item):
        uri=gnomevfs.get_uri_from_local_path(item.filename)
        mime=gnomevfs.get_mime_type(uri)
        cmd=None
        if mime in settings.custom_launchers:
            for app in settings.custom_launchers[mime]:
                from string import Template
                fullpath=item.filename
                directory=os.path.split(item.filename)[0]
                fullname=os.path.split(item.filename)[1]
                name=os.path.splitext(fullname)[0]
                ext=os.path.splitext(fullname)[1]
                cmd=Template(app[1]).substitute(
                    {'FULLPATH':fullpath,'DIR':directory,'FULLNAME':fullname,'NAME':name,'EXT':ext})
                break
        if not cmd:
            for app in gnomevfs.mime_get_all_applications(mime):
                cmd=app[2]+' "%s"'%(item.filename,)
        if cmd:
            print 'mime_open',cmd
            subprocess.Popen(cmd,shell=True)
        else:
            print 'no known command for ',item.filename,' mimetype',mime

    def edit_item(self,widget,item):
        self.dlg=metadatadialogs.MetaDialog(item)
        self.dlg.show()

    def rotate_item_left(self,widget,item):
        ##TODO: put this task in the background thread (using the recreate thumb job)
        imagemanip.rotate_left(item)
        self.update_required_thumbs()
        if item==self.focal_item:
            self.view_image(item)

    def rotate_item_right(self,widget,item):
        ##TODO: put this task in the background thread (using the recreate thumb job)
        imagemanip.rotate_right(item)
        self.update_required_thumbs()
        if item==self.focal_item:
            self.view_image(item)

    def delete_item(self,widget,item):
        fileops.worker.delete([item],None,False)
        ind=self.tm.view.find_item(item)
        if ind>=0:
            self.tm.view.del_item(item)
            if self.is_iv_showing:
                ind=min(ind,len(self.tm.view)-1)
                self.view_image(self.tm.view(ind))
        elif self.is_iv_showing:
            self.hide_image()
        self.refresh_view()

    def item_to_view_index(self,item):
        return self.tm.view.find_item(item)

    def item_to_scroll_value(self,item):
        ind=self.item_to_view_index(item)
        return max(0,ind*(self.geo_thumbheight+self.geo_pad)/self.geo_horiz_count)#-self.geo_width/2)

    def multi_select(self,ind_from,ind_to,select=True):
        '''select multiple items in a given array subscript range of the view'''
        ##todo: handle tag mode?
        self.tm.lock.acquire()
        if ind_to>ind_from:
            for x in range(ind_from,ind_to+1):
                item=self.tm.view(x)
                if not item.selected and select:
                    self.tm.collection.numselected+=1
                if item.selected and not select:
                    self.tm.collection.numselected-=1
                item.selected=select
        else:
            for x in range(ind_from,ind_to-1,-1):
                item=self.tm.view(x)
                if not item.selected and select:
                    self.tm.collection.numselected+=1
                if item.selected and not select:
                    self.tm.collection.numselected-=1
                item.selected=select
        self.tm.lock.release()
        self.update_info_bar()
        self.redraw_view()

    def select_item(self,ind):
        '''select an item by array index of the view. in tag mode, toggles
        whatever tags are checked in the tag pane'''
        if 0<=ind<len(self.tm.view):
            item=self.tm.view(ind)
            if self.mode==self.MODE_TAG:
                tags=self.tagframe.get_checked_tags()
                imageinfo.toggle_tags(item,tags)
            elif self.mode==self.MODE_NORMAL:
                if item.selected:
                    self.tm.collection.numselected-=1
                else:
                    self.tm.collection.numselected+=1
                item.selected=not item.selected
            self.last_selected=item
            self.last_selected_ind=ind
            self.update_info_bar()
            self.redraw_view()

    def button_press(self,obj,event):
        '''callback for mouse button presses (handles selections, view double clicks,
            context menu right clicks, mouse overlay clicks)'''
        print 'press',event.button,event.type
        self.imarea.grab_focus()
        self.lock.acquire()
        ind=(int(self.geo_view_offset)+int(event.y))/(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count
        ind+=min(self.geo_horiz_count,int(event.x)/(self.geo_thumbwidth+self.geo_pad))
        ind=max(0,min(len(self.tm.view)-1,ind))
        item=self.tm.view(ind)
        if event.button==1 and event.type==gtk.gdk._2BUTTON_PRESS:
##            if ind==self.pressed_ind and self.tm.view(ind)==self.pressed_item and event.x<=(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count:
                self.emit("activate-item",ind,item)
                self.button_press_block=True
                if item==self.pressed_item:
                    self.select_item(self.pressed_ind)
        elif event.button==1 and event.type==gtk.gdk.BUTTON_RELEASE:
                if not self.button_press_block:
                    self.drop_item=item
                    cmd=self.get_hover_command(ind,event.x,event.y)
                    if cmd>=0:
                        if ind==self.pressed_ind and item==self.pressed_item and event.x<=(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count:
                            self.hover_cmds[cmd][0](None,self.pressed_item)
                    else:
                        if self.last_selected and event.state&(gtk.gdk.SHIFT_MASK|gtk.gdk.CONTROL_MASK):
                            ind=self.item_to_view_index(self.last_selected)
                            if ind>=0:
                                self.multi_select(ind,self.pressed_ind,bool(event.state&gtk.gdk.SHIFT_MASK))
                        else:
                            if item==self.pressed_item:
                                self.select_item(self.pressed_ind)
                self.button_press_block=False
        elif event.button==3 and event.type==gtk.gdk.BUTTON_RELEASE:
            self.emit("context-click-item",ind,item)
        if event.button==1 and event.type in (gtk.gdk.BUTTON_PRESS,gtk.gdk._2BUTTON_PRESS):
            self.drag_item=item
            self.pressed_ind=ind
            self.pressed_item=self.tm.view(ind)
        else:
            self.pressed_ind=-1
            self.pressed_item=None
        self.lock.release()

    def drag_begin_signal(self, widget, drag_context):
        pass
#        self.drag_item=None
#        x,y=self.imarea.get_pointer()
#        print 'drag begin',x,y
#        if not (0<=x<self.geo_width or 0<=y<self.geo_height):
#            return False
#        ind=(int(self.geo_view_offset)+int(y))/(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count
#        ind+=min(self.geo_horiz_count,int(x)/(self.geo_thumbwidth+self.geo_pad))
#        if 0<=ind<len(self.tm.view):
#            self.drag_item=self.tm.view(ind)
#            return True
#        else:
#            self.drag_item=None
#            return False

    def drag_receive_signal(self, widget, drag_context, x, y, selection_data, info, timestamp):
        '''callback triggered to retrieve the selection_data payload
        (viewer is the destination of the drop)'''
        if selection_data.type=='tag-tree-row':
            ind=(int(self.geo_view_offset)+int(y))/(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count
            ind+=min(self.geo_horiz_count,int(x)/(self.geo_thumbwidth+self.geo_pad))
            ind=max(0,min(len(self.tm.view)-1,ind))
            item=self.tm.view(ind)
            data=selection_data.data
            paths=data.split('-')
            tags=self.tagframe.get_tags(paths[0])
            if not item.selected:
                imageinfo.toggle_tags(item,tags)
            else:
                self.tm.keyword_edit(tags,True)
            return
        uris=selection_data.get_uris()
        if uris: ##todo: do we  actually want to process dropped uris? don't forget to ignore drops from self
            for uri in uris:
                print 'dropped uris',uris

    def drag_get_signal(self, widget, drag_context, selection_data, info, timestamp):
        '''callback triggered to set the selection_data payload
        (viewer is the source of the drop)'''
        if self.drag_item==None:
            return
        selection_data.set('image-filename', 8, self.drag_item.filename)
        if not self.drag_item.selected:
            uri=gnomevfs.get_uri_from_local_path(self.drag_item.filename)
            selection_data.set_uris([uri])
        else:
            uris=[]
            i=0
            while i<len(self.tm.view):
                item=self.tm.view(i)
                if item.selected:
                    uri=gnomevfs.get_uri_from_local_path(item.filename)
                    uris.append(uri)
                i+=1
            selection_data.set_uris(uris)
        print 'dragging selected uris',selection_data.get_uris()
        self.drag_item=None

    def recalc_hover_ind(self,x,y):
        '''return the index of the item of the drawable coordinates (x,y)'''
        ind=(int(self.geo_view_offset)+int(y))/(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count
        ind+=min(self.geo_horiz_count,int(x)/(self.geo_thumbwidth+self.geo_pad))
        ind=max(0,min(len(self.tm.view)-1,ind))
        if x>=(self.geo_thumbheight+self.geo_pad)*self.geo_horiz_count:
            ind=-1
        return ind

    def mouse_motion_signal(self,obj,event):
        '''callback when mouse moves in the viewer area (updates image overlay as necessary)'''
        ind=self.recalc_hover_ind(event.x,event.y)
        if self.hover_ind!=ind:
            self.hover_ind=ind
            self.redraw_view()

    def mouse_leave_signal(self,obj,event):
        '''callback when mouse leaves the viewer area (hides image overlays)'''
        if self.hover_ind>=0:
            self.hover_ind=-1
            self.redraw_view()

    def redraw_view(self):
        '''redraw the view without recomputing geometry or changing position'''
#        self.refresh_view()
        self.imarea.window.invalidate_rect((0,0,self.geo_width,self.geo_height),True)

    def update_info_bar(self):
        '''refresh the info bar (status bar that displays number of images etc)'''
        pass
        ##todo: send a signal about collection updates
        #self.info_bar.set_label('%i images in collection (%i selected, %i in view)'%(len(self.tm.collection),self.tm.collection.numselected,len(self.tm.view)))

    def refresh_view(self):
        '''update geometry, scrollbars, redraw the thumbnail view'''
        self.update_geometry()
        self.update_required_thumbs()
        self.update_scrollbar()
        self.update_info_bar()
        self.imarea.window.invalidate_rect((0,0,self.geo_width,self.geo_height),True)

    def post_build_view(self):
        '''callback function to receive notification from worker that
        view has finished rebuilding'''
        pass
##      send a custom signal
#        self.tagframe.refresh(self.tm.view.tag_cloud)

    def update_view(self):
        '''reset position, update geometry, scrollbars, redraw the thumbnail view'''
        self.geo_view_offset=0
        self.refresh_view()

    def scroll_signal_pane(self,obj,event):
        '''scrolls the view on mouse wheel motion'''
        if event.direction==gtk.gdk.SCROLL_UP:
            self.scroll_up(max(1,self.geo_thumbheight+self.geo_pad)/5)
        if event.direction==gtk.gdk.SCROLL_DOWN:
            self.scroll_down(max(1,self.geo_thumbheight+self.geo_pad)/5)

    def scroll_signal(self,obj):
        '''signal response when the scroll position changes'''
        self.geo_view_offset=self.scrolladj.get_value()
#        self.update_geometry()
        self.update_view_index_range()
        self.update_required_thumbs()
        self.imarea.window.invalidate_rect((0,0,self.geo_width,self.geo_height),True)
        self.vscroll.trigger_tooltip_query()

    def update_scrollbar(self):
        '''called to resync the scrollbar to changes in view geometry'''
        upper=len(self.tm.view)/self.geo_horiz_count
        if len(self.tm.view)%self.geo_horiz_count!=0:
            upper+=1
        upper=upper*(self.geo_thumbheight+self.geo_pad)
        self.scrolladj.set_all(value=self.geo_view_offset, lower=0,
                upper=upper,
                step_increment=max(1,self.geo_thumbheight+self.geo_pad)/5,
                page_increment=self.geo_height, page_size=self.geo_height)

    def scroll_up(self,step=10):
        '''call to scroll the view up by step pixels'''
        self.vscroll.set_value(self.vscroll.get_value()-step)

    def scroll_down(self,step=10):
        '''call to scroll the view down by step pixels'''
        self.vscroll.set_value(self.vscroll.get_value()+step)

    def configure_geometry(self):
        '''first time initialization of geometry (called from __init__)'''
        self.geo_thumbwidth=128
        self.geo_thumbheight=128
        if settings.maemo:
            self.geo_pad=20
        else:
            self.geo_pad=30
        self.geo_view_offset=0
        self.geo_screen_offset=0
        self.geo_ind_view_first=0
        self.geo_ind_view_last=0
        self.geo_horiz_count=1

    def update_view_index_range(self):
        '''computes the first and last indices in the view'''
        self.geo_ind_view_first = int(self.geo_view_offset/(self.geo_thumbheight+self.geo_pad))*self.geo_horiz_count
        self.geo_ind_view_last = self.geo_ind_view_first+self.geo_horiz_count*(2+self.geo_height/(self.geo_thumbheight+self.geo_pad))

    def update_geometry(self,recenter=False):
        '''recompute the changeable parts of the geometry (usually called in response to window
           size changes, or changes to the number of items in the collection'''
        nudge=self.calc_screen_offset()
        self.geo_horiz_count=max(int(self.geo_width/(self.geo_thumbwidth+self.geo_pad)),1)
        self.geo_view_offset_max=max(1,(self.geo_thumbheight+self.geo_pad)+(len(self.tm.view)-1)*(self.geo_thumbheight+self.geo_pad)/self.geo_horiz_count)
        self.geo_view_offset=max(0,min(self.geo_view_offset_max-self.geo_height,self.geo_view_offset))
        if recenter:
            if self.focal_item!=None:
                ind=self.item_to_view_index(self.focal_item)
            else:
                ind=-1
            if self.geo_ind_view_first<=ind<=self.geo_ind_view_last:
                self.center_view_offset(ind)
            else:
                self.set_view_offset(self.geo_ind_view_first)
                self.geo_view_offset-=nudge
        #print 'geo',self.geo_view_offset
        self.update_view_index_range()

    def update_required_thumbs(self):
        onscreen_items=self.tm.view.get_items(self.geo_ind_view_first,self.geo_ind_view_last)
        self.tm.request_thumbnails(onscreen_items) ##todo: caching ,fore_items,back_items

    def calc_screen_offset(self):
        '''computes how much to offset the first item in the view from the top of the screen (this should be negative)'''
        return int(self.geo_ind_view_first/self.geo_horiz_count)*(self.geo_pad+self.geo_thumbheight)-self.geo_view_offset

    def set_view_offset(self,index):
        '''reset the view offset position to keep the first item on screen after a window size change'''
        self.geo_view_offset=int(index/self.geo_horiz_count)*(self.geo_pad+self.geo_thumbheight)+self.geo_screen_offset
        self.geo_view_offset=max(0,min(self.geo_view_offset_max-self.geo_height,self.geo_view_offset))

    def center_view_offset(self,index):
        '''center the view on particular item in the view (receives an index)'''
        self.geo_screen_offset=0
        self.geo_view_offset=int(index/self.geo_horiz_count)*(self.geo_pad+self.geo_thumbheight)-self.geo_height/2+(self.geo_pad+self.geo_thumbheight)/2
        self.geo_view_offset=max(0,min(self.geo_view_offset_max-self.geo_height,self.geo_view_offset))

    def configure_signal(self,obj,event):
        '''received when the window size of the drawing area changes'''
        self.geo_width=event.width
        self.geo_height=event.height
        self.update_geometry(True)
        self.update_scrollbar()
        self.update_required_thumbs()
        self.imarea.grab_focus()
##        self.imarea.window.invalidate_rect((0,0,self.geo_width,self.geo_height),True)

    def expose_signal(self,event,arg):
        '''received when part of the drawing area needs to be shown'''
        self.realize_signal(event)

    def realize_signal(self,event):
        '''renders the view - received when the drawing area needs to be shown'''
        self.lock.acquire()
        drawable = self.imarea.window
        gc = drawable.new_gc()
        colormap=drawable.get_colormap()
        grey = colormap.alloc('grey')
        gc_s = drawable.new_gc(foreground=grey,background=grey)
        white = colormap.alloc('white')
        gc_v = drawable.new_gc(foreground=white)
        colormap=drawable.get_colormap()
        black = colormap.alloc('black')
        green = colormap.alloc('green')
        gc_g = drawable.new_gc(foreground=green)
        red= colormap.alloc('red')
        gc_r = drawable.new_gc(foreground=red)

        drawable.set_background(black)

        (mx,my)=self.imarea.get_pointer()
        if 0<=mx<drawable.get_size()[0] and 0<=my<drawable.get_size()[1]:
            self.hover_ind=self.recalc_hover_ind(mx,my)
        else:
            self.hover_ind=-1

        ##TODO: USE draw_drawable to shift screen for small moves in the display (scrolling)
        display_space=True
        imgind=self.geo_ind_view_first
        x=0
        y=self.calc_screen_offset()
        drawable.clear()
        i=imgind
        neededitem=None
        while i<self.geo_ind_view_last:
            if 0<=i<len(self.tm.view):
                item=self.tm.view(i)
            else:
                break
            if self.last_selected_ind>=0 and self.hover_ind>=0 and (self.last_selected_ind>=i>=self.hover_ind or self.last_selected_ind<=i<=self.hover_ind):
                key_mods=gtk.gdk.display_get_default().get_pointer()[3]
                if key_mods&(gtk.gdk.SHIFT_MASK|gtk.gdk.CONTROL_MASK):
                    if self.last_selected:
                        if key_mods&gtk.gdk.SHIFT_MASK:
                            drawable.draw_rectangle(gc_g, True, x+self.geo_pad/16, y+self.geo_pad/16, self.geo_thumbwidth+self.geo_pad*7/8, self.geo_thumbheight+self.geo_pad*7/8)
                        else:
                            drawable.draw_rectangle(gc_r, True, x+self.geo_pad/16, y+self.geo_pad/16, self.geo_thumbwidth+self.geo_pad*7/8, self.geo_thumbheight+self.geo_pad*7/8)
            if item.selected:
                drawable.draw_rectangle(gc_s, True, x+self.geo_pad/8, y+self.geo_pad/8, self.geo_thumbwidth+self.geo_pad*3/4, self.geo_thumbheight+self.geo_pad*3/4)
#           todo: come up with a scheme for highlighting images
##            if item==self.focal_item:
##                try:
##                    (thumbwidth,thumbheight)=self.tm.view(i).thumbsize
##                    adjy=self.geo_pad/2+(128-thumbheight)/2-3
##                    adjx=self.geo_pad/2+(128-thumbwidth)/2-3
##                    drawable.draw_rectangle(gc_v, True, x+adjx, y+adjy, thumbwidth+6, thumbheight+6)
##                except:
##                    pass
#            drawable.draw_rectangle(gc, True, x+self.geo_pad/4, y+self.geo_pad/4, self.geo_thumbwidth+self.geo_pad/2, self.geo_thumbheight+self.geo_pad/2)
            fail_item=False
            if item.thumb:
                (thumbwidth,thumbheight)=self.tm.view(i).thumbsize
                adjy=self.geo_pad/2+(128-thumbheight)/2
                adjx=self.geo_pad/2+(128-thumbwidth)/2
                drawable.draw_pixbuf(gc, item.thumb, 0, 0,x+adjx,y+adjy)
            elif item.cannot_thumb:
                adjy=self.geo_pad/2
                adjx=self.geo_pad/2
                drawable.draw_pixbuf(gc, self.pixbuf_thumb_fail, 0, 0,x+adjx,y+adjy)
            else:
                adjy=self.geo_pad/2
                adjx=self.geo_pad/2
                drawable.draw_pixbuf(gc, self.pixbuf_thumb_load, 0, 0,x+adjx,y+adjy)
            if self.hover_ind==i or item.meta_changed or item.selected or fail_item:
                if self.hover_ind==i or item.selected:
                    a,b=imageinfo.text_descr(item)
                    l=self.imarea.create_pango_layout('')
                    l.set_markup('<b><big>'+a+'</big></b>\n'+b)
                    drawable.draw_layout(gc,x+self.geo_pad/4,y+self.geo_pad+self.geo_thumbheight-l.get_pixel_size()[1]-self.geo_pad/4,l,white)
#                    print imageinfo.text_descr(item)
                l=len(self.hover_cmds)
                offx=self.geo_pad/4
                offy=self.geo_pad/4
##                show=[True for r in range(l)]
##                if self.hover_ind!=i:
##                    for q in (1,2,3,4,5,6):
##                        show[q]=False
#               todo: create a callback for each item to see if it should be shown
##                if not item.meta_changed:
##                    show[0]=False
##                    show[1]=False
                for j in range(l):
                    if show[j]:
                        drawable.draw_pixbuf(gc,self.hover_cmds[j][1],0,0,x+offx,y+offy)
                    offx+=self.hover_cmds[j][1].get_width()+self.geo_pad/4
            i+=1
            x+=self.geo_thumbwidth+self.geo_pad
            if x+self.geo_thumbwidth+self.geo_pad>=self.geo_width:
                y+=self.geo_thumbheight+self.geo_pad
                if y>=self.geo_height+self.geo_pad:
                    break
                else:
                    x=0
        self.lock.release()

gobject.type_register(ImageBrowser)