import collections
import itertools
import os
import random
import time
import typing

from qtpy import QtCore as QC
from qtpy import QtWidgets as QW
from qtpy import QtGui as QG

from hydrus.core import HydrusConstants as HC
from hydrus.core import HydrusData
from hydrus.core import HydrusExceptions
from hydrus.core import HydrusGlobals as HG
from hydrus.core import HydrusPaths
from hydrus.core import HydrusTime
from hydrus.core.files.images import HydrusImageHandling
from hydrus.core.networking import HydrusNetwork

from hydrus.client import ClientApplicationCommand as CAC
from hydrus.client import ClientConstants as CC
from hydrus.client import ClientData
from hydrus.client import ClientFiles
from hydrus.client import ClientGlobals as CG
from hydrus.client import ClientLocation
from hydrus.client import ClientPaths
from hydrus.client.gui import ClientGUIDragDrop
from hydrus.client.gui import ClientGUICore as CGC
from hydrus.client.gui import ClientGUIDialogs
from hydrus.client.gui import ClientGUIDialogsManage
from hydrus.client.gui import ClientGUIDialogsMessage
from hydrus.client.gui import ClientGUIDialogsQuick
from hydrus.client.gui import ClientGUIDuplicates
from hydrus.client.gui import ClientGUIFunctions
from hydrus.client.gui import ClientGUIMenus
from hydrus.client.gui import ClientGUIScrolledPanelsEdit
from hydrus.client.gui import ClientGUIScrolledPanelsManagement
from hydrus.client.gui import ClientGUIShortcuts
from hydrus.client.gui import ClientGUITags
from hydrus.client.gui import ClientGUITopLevelWindowsPanels
from hydrus.client.gui import QtPorting as QP
from hydrus.client.gui.canvas import ClientGUICanvas
from hydrus.client.gui.canvas import ClientGUICanvasFrame
from hydrus.client.gui.exporting import ClientGUIExport
from hydrus.client.gui.media import ClientGUIMediaSimpleActions
from hydrus.client.gui.media import ClientGUIMediaModalActions
from hydrus.client.gui.media import ClientGUIMediaMenus
from hydrus.client.gui.networking import ClientGUIHydrusNetwork
from hydrus.client.gui.pages import ClientGUIManagementController
from hydrus.client.media import ClientMedia
from hydrus.client.media import ClientMediaFileFilter
from hydrus.client.metadata import ClientContentUpdates
from hydrus.client.metadata import ClientTags

MAC_QUARTZ_OK = True

if HC.PLATFORM_MACOS:
    
    try:
        
        from hydrus.client import ClientMacIntegration
        
    except:
        
        MAC_QUARTZ_OK = False
        
    

FRAME_DURATION_60FPS = 1.0 / 60

class ThumbnailWaitingToBeDrawn( object ):
    
    def __init__( self, hash, thumbnail, thumbnail_index, bitmap ):
        
        self.hash = hash
        self.thumbnail = thumbnail
        self.thumbnail_index = thumbnail_index
        self.bitmap = bitmap
        
        self._draw_complete = False
        
    
    def DrawComplete( self ) -> bool:
        
        return self._draw_complete
        
    
    def DrawDue( self ) -> bool:
        
        return True
        
    
    def DrawToPainter( self, x: int, y: int, painter: QG.QPainter ):
        
        painter.drawImage( x, y, self.bitmap )
        
        self._draw_complete = True
        
    

class ThumbnailWaitingToBeDrawnAnimated( ThumbnailWaitingToBeDrawn ):
    
    FADE_DURATION_S = 0.5
    
    def __init__( self, hash, thumbnail, thumbnail_index, bitmap ):
        
        ThumbnailWaitingToBeDrawn.__init__( self, hash, thumbnail, thumbnail_index, bitmap )
        
        self.num_frames_drawn = 0
        self.num_frames_to_draw = max( int( self.FADE_DURATION_S // FRAME_DURATION_60FPS ), 1 ) 
        
        opacity_factor = max( 0.05, 1 / ( self.num_frames_to_draw / 3 ) )
        
        self.alpha_bmp = QP.AdjustOpacity( self.bitmap, opacity_factor )
        
        self.animation_started_precise = HydrusTime.GetNowPrecise()
        
    
    def _GetNumFramesOutstanding( self ):
        
        now_precise = HydrusTime.GetNowPrecise()
        
        num_frames_to_now = int( ( now_precise - self.animation_started_precise ) // FRAME_DURATION_60FPS )
        
        return min( num_frames_to_now, self.num_frames_to_draw - self.num_frames_drawn )
        
    
    def DrawDue( self ) -> bool:
        
        return self._GetNumFramesOutstanding() > 0
        
    
    def DrawToPainter( self, x: int, y: int, painter: QG.QPainter ):
        
        num_frames_to_draw = self._GetNumFramesOutstanding()
        
        if self.num_frames_drawn + num_frames_to_draw >= self.num_frames_to_draw:
            
            painter.drawImage( x, y, self.bitmap )
            
            self.num_frames_drawn = self.num_frames_to_draw
            self._draw_complete = True
            
        else:
            
            for i in range( num_frames_to_draw ):
                
                painter.drawImage( x, y, self.alpha_bmp )
                
            
            self.num_frames_drawn += num_frames_to_draw
            
        
    

class MediaPanel( CAC.ApplicationCommandProcessorMixin, ClientMedia.ListeningMediaList, QW.QScrollArea ):
    
    selectedMediaTagPresentationChanged = QC.Signal( list, bool )
    selectedMediaTagPresentationIncremented = QC.Signal( list )
    statusTextChanged = QC.Signal( str )
    
    focusMediaChanged = QC.Signal( ClientMedia.Media )
    focusMediaCleared = QC.Signal()
    focusMediaPaused = QC.Signal()
    refreshQuery = QC.Signal()
    
    newMediaAdded = QC.Signal()
    
    def __init__( self, parent, page_key, management_controller: ClientGUIManagementController.ManagementController, media_results ):
        
        QW.QScrollArea.__init__( self, parent )
        
        self.setFrameStyle( QW.QFrame.Panel | QW.QFrame.Sunken )
        self.setLineWidth( 2 )
        
        self.resize( QC.QSize( 20, 20 ) )
        self.setWidget( QW.QWidget( self ) )
        self.setWidgetResizable( True )
        
        self._page_key = page_key
        self._management_controller = management_controller
        
        ClientMedia.ListeningMediaList.__init__( self, self._management_controller.GetLocationContext(), media_results )
        CAC.ApplicationCommandProcessorMixin.__init__( self )
        
        self._UpdateBackgroundColour()
        
        self.verticalScrollBar().setSingleStep( 50 )
        
        self._focused_media = None
        self._last_hit_media = None
        self._next_best_media_if_focuses_removed = None
        self._shift_select_started_with_this_media = None
        self._media_added_in_current_shift_select = set()
        
        self._empty_page_status_override = None
        
        CG.client_controller.sub( self, 'AddMediaResults', 'add_media_results' )
        CG.client_controller.sub( self, 'RemoveMedia', 'remove_media' )
        CG.client_controller.sub( self, '_UpdateBackgroundColour', 'notify_new_colourset' )
        CG.client_controller.sub( self, 'SelectByTags', 'select_files_with_tags' )
        CG.client_controller.sub( self, 'LaunchMediaViewerOnFocus', 'launch_media_viewer' )
        
        self._had_changes_to_tag_presentation_while_hidden = False
        
        self._my_shortcut_handler = ClientGUIShortcuts.ShortcutsHandler( self, [ 'media', 'thumbnails' ] )
        
        self.setWidget( self._InnerWidget( self ) )
        self.setWidgetResizable( True )
        
    
    def __bool__( self ):
        
        return QP.isValid( self )
        
    
    def _Archive( self ):
        
        hashes = self._GetSelectedHashes( discriminant = CC.DISCRIMINANT_INBOX )
        
        if len( hashes ) > 0:
            
            if HC.options[ 'confirm_archive' ]:
                
                if len( hashes ) > 1:
                    
                    message = 'Archive ' + HydrusData.ToHumanInt( len( hashes ) ) + ' files?'
                    
                    result = ClientGUIDialogsQuick.GetYesNo( self, message )
                    
                    if result != QW.QDialog.Accepted:
                        
                        return
                        
                    
                
            
            CG.client_controller.Write( 'content_updates', ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( CC.COMBINED_LOCAL_FILE_SERVICE_KEY, ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_ARCHIVE, hashes ) ) )
            
        
    
    def _ArchiveDeleteFilter( self ):
        
        if len( self._selected_media ) == 0:
            
            media_results = self.GenerateMediaResults( discriminant = CC.DISCRIMINANT_LOCAL_BUT_NOT_IN_TRASH, selected_media = set( self._sorted_media ), for_media_viewer = True )
            
        else:
            
            media_results = self.GenerateMediaResults( discriminant = CC.DISCRIMINANT_LOCAL_BUT_NOT_IN_TRASH, selected_media = set( self._selected_media ), for_media_viewer = True )
            
        
        if len( media_results ) > 0:
            
            self.SetFocusedMedia( None )
            
            canvas_frame = ClientGUICanvasFrame.CanvasFrame( self.window() )
            
            canvas_window = ClientGUICanvas.CanvasMediaListFilterArchiveDelete( canvas_frame, self._page_key, self._location_context, media_results )
            
            canvas_frame.SetCanvas( canvas_window )
            
            canvas_window.exitFocusMedia.connect( self.SetFocusedMedia )
            
        
    
    def _ClearDeleteRecord( self ):
        
        media = self._GetSelectedFlatMedia()
        
        ClientGUIMediaModalActions.ClearDeleteRecord( self, media )
        
    
    def _Delete( self, file_service_key = None, only_those_in_file_service_key = None ):
        
        if file_service_key is None:
            
            if len( self._location_context.current_service_keys ) == 1:
                
                ( possible_suggested_file_service_key, ) = self._location_context.current_service_keys
                
                if CG.client_controller.services_manager.GetServiceType( possible_suggested_file_service_key ) in HC.SPECIFIC_LOCAL_FILE_SERVICES + ( HC.FILE_REPOSITORY, ):
                    
                    file_service_key = possible_suggested_file_service_key
                    
                
            
        
        media_to_delete = ClientMedia.FlattenMedia( self._selected_media )
        
        if only_those_in_file_service_key is not None:
            
            media_to_delete = ClientMedia.FlattenMedia( media_to_delete )
            
            media_to_delete = [ m for m in media_to_delete if only_those_in_file_service_key in m.GetLocationsManager().GetCurrent() ]
            
        
        if file_service_key is None or CG.client_controller.services_manager.GetServiceType( file_service_key ) in HC.LOCAL_FILE_SERVICES:
            
            default_reason = 'Deleted from Media Page.'
            
        else:
            
            default_reason = 'admin'
            
        
        try:
            
            ( hashes_physically_deleted, content_update_packages ) = ClientGUIDialogsQuick.GetDeleteFilesJobs( self, media_to_delete, default_reason, suggested_file_service_key = file_service_key )
            
        except HydrusExceptions.CancelledException:
            
            return
            
        
        if len( hashes_physically_deleted ) > 0:
            
            self._RemoveMediaByHashes( hashes_physically_deleted )
            
        
        def do_it( content_update_packages ):
            
            for content_update_package in content_update_packages:
                
                CG.client_controller.WriteSynchronous( 'content_updates', content_update_package )
                
            
        
        CG.client_controller.CallToThread( do_it, content_update_packages )
        
    
    def _DeselectSelect( self, media_to_deselect, media_to_select ):
        
        if len( media_to_deselect ) > 0:
            
            for m in media_to_deselect: m.Deselect()
            
            self._RedrawMedia( media_to_deselect )
            
            self._selected_media.difference_update( media_to_deselect )
            
        
        if len( media_to_select ) > 0:
            
            for m in media_to_select: m.Select()
            
            self._RedrawMedia( media_to_select )
            
            self._selected_media.update( media_to_select )
            
        
        self._PublishSelectionChange()
        
    
    def _DownloadSelected( self ):
        
        hashes = self._GetSelectedHashes( discriminant = CC.DISCRIMINANT_NOT_LOCAL )
        
        self._DownloadHashes( hashes )
        
    
    def _DownloadHashes( self, hashes ):
        
        CG.client_controller.quick_download_manager.DownloadFiles( hashes )
        
    
    def _EndShiftSelect( self ):
        
        self._shift_select_started_with_this_media = None
        self._media_added_in_current_shift_select = set()
        
    
    def _GetFocusSingleton( self ) -> ClientMedia.MediaSingleton:
        
        if self._focused_media is not None:
            
            media_singleton = self._focused_media.GetDisplayMedia()
            
            if media_singleton is not None:
                
                return media_singleton
                
            
        
        raise HydrusExceptions.DataMissing( 'No media singleton!' )
        
    
    def _GetMediasForFileCommandTarget( self, file_command_target: int ) -> typing.Collection[ ClientMedia.MediaSingleton ]:
        
        if file_command_target == CAC.FILE_COMMAND_TARGET_FOCUSED_FILE:
            
            if self._HasFocusSingleton():
                
                media = self._GetFocusSingleton()
                
                return [ media.GetDisplayMedia() ]
                
            
        elif file_command_target == CAC.FILE_COMMAND_TARGET_SELECTED_FILES:
            
            if len( self._selected_media ) > 0:
                
                medias = self._GetSelectedMediaOrdered()
                
                return ClientMedia.FlattenMedia( medias )
                
            
        
        return []
        
    
    def _GetNumSelected( self ):
        
        return sum( [ media.GetNumFiles() for media in self._selected_media ] )
        
    
    def _GetPrettyStatusForStatusBar( self ) -> str:
        
        num_files = len( self._hashes )
        
        if self._empty_page_status_override is not None:
            
            if num_files == 0:
                
                return self._empty_page_status_override
                
            else:
                
                # user has dragged files onto this page or similar
                
                self._empty_page_status_override = None
                
            
        
        num_selected = self._GetNumSelected()
        
        num_files_string = ClientMedia.GetMediasFiletypeSummaryString( self._sorted_media )
        selected_files_string = ClientMedia.GetMediasFiletypeSummaryString( self._selected_media )
        
        s = num_files_string # 23 files
        
        if num_selected == 0:
            
            if num_files > 0:
                
                pretty_total_size = self._GetPrettyTotalSize()
                
                s += ' - totalling ' + pretty_total_size
                
                pretty_total_duration = self._GetPrettyTotalDuration()
                
                if pretty_total_duration != '':
                    
                    s += ', {}'.format( pretty_total_duration )
                    
                
            
        else:
            
            s += ' - '
            
            # if 1 selected, we show the whole mime string, so no need to specify
            if num_selected == 1 or selected_files_string == num_files_string:
                
                selected_files_string = HydrusData.ToHumanInt( num_selected )
                
            
            if num_selected == 1: # 23 files - 1 video selected, file_info
                
                ( selected_media, ) = self._selected_media
                
                pretty_info_lines = [ line for line in selected_media.GetPrettyInfoLines( only_interesting_lines = True ) if isinstance( line, str ) ]
                
                s += '{} selected, {}'.format( selected_files_string, ', '.join( pretty_info_lines ) )
                
            else: # 23 files - 5 selected, selection_info
                
                num_inbox = sum( ( media.GetNumInbox() for media in self._selected_media ) )
                
                if num_inbox == num_selected:
                    
                    inbox_phrase = 'all in inbox'
                    
                elif num_inbox == 0:
                    
                    inbox_phrase = 'all archived'
                    
                else:
                    
                    inbox_phrase = '{} in inbox and {} archived'.format( HydrusData.ToHumanInt( num_inbox ), HydrusData.ToHumanInt( num_selected - num_inbox ) )
                    
                
                pretty_total_size = self._GetPrettyTotalSize( only_selected = True )
                
                s += '{} selected, {}, totalling {}'.format( selected_files_string, inbox_phrase, pretty_total_size )
                
                pretty_total_duration = self._GetPrettyTotalDuration( only_selected = True )
                
                if pretty_total_duration != '':
                    
                    s += ', {}'.format( pretty_total_duration )
                    
                
            
        
        return s
        
    
    def _GetPrettyTotalDuration( self, only_selected = False ):
        
        if only_selected:
            
            media_source = self._selected_media
            
        else:
            
            media_source = self._sorted_media
            
        
        if len( media_source ) == 0 or False in ( media.HasDuration() for media in media_source ):
            
            return ''
            
        
        total_duration = sum( ( media.GetDurationMS() for media in media_source ) )
        
        return HydrusTime.MillisecondsDurationToPrettyTime( total_duration )
        
    
    def _GetPrettyTotalSize( self, only_selected = False ):
        
        if only_selected:
            
            media_source = self._selected_media
            
        else:
            
            media_source = self._sorted_media
            
        
        total_size = sum( [ media.GetSize() for media in media_source ] )
        
        unknown_size = False in ( media.IsSizeDefinite() for media in media_source )
        
        if total_size == 0:
            
            if unknown_size:
                
                return 'unknown size'
                
            else:
                
                return HydrusData.ToHumanBytes( 0 )
                
            
        else:
            
            if unknown_size:
                
                return HydrusData.ToHumanBytes( total_size ) + ' + some unknown size'
                
            else:
                
                return HydrusData.ToHumanBytes( total_size )
                
            
        
    
    def _GetSelectedHashes( self, is_in_file_service_key = None, discriminant = None, is_not_in_file_service_key = None, ordered = False ):
        
        if ordered:
            
            result = []
            
            for media in self._GetSelectedMediaOrdered():
                
                result.extend( media.GetHashes( is_in_file_service_key, discriminant, is_not_in_file_service_key, ordered ) )
                
            
        else:
            
            result = set()
            
            for media in self._selected_media:
                
                result.update( media.GetHashes( is_in_file_service_key, discriminant, is_not_in_file_service_key, ordered ) )
                
            
        
        return result
        
    
    def _GetSelectedCollections( self ):
        
        sorted_selected_collections = [ media for media in self._sorted_media if media.IsCollection() and media in self._selected_media ]
        
        return sorted_selected_collections
        

    def _GetSelectedFlatMedia( self, is_in_file_service_key = None, discriminant = None, is_not_in_file_service_key = None ):
        
        # this now always delivers sorted results
        
        sorted_selected_media = [ media for media in self._sorted_media if media in self._selected_media ]
        
        flat_media = ClientMedia.FlattenMedia( sorted_selected_media )
        
        flat_media = [ media for media in flat_media if media.MatchesDiscriminant( is_in_file_service_key = is_in_file_service_key, discriminant = discriminant, is_not_in_file_service_key = is_not_in_file_service_key ) ]
        
        return flat_media
        
    
    def _GetSelectedMediaOrdered( self ):
        
        # note that this is fast because sorted_media is custom
        return sorted( self._selected_media, key = lambda m: self._sorted_media.index( m ) )
        
    
    def _GetSortedSelectedMimeDescriptors( self ):
        
        def GetDescriptor( plural, classes, num_collections ):
            
            suffix = 's' if plural else ''
            
            if len( classes ) == 0:
                
                return 'file' + suffix
                
            
            if len( classes ) == 1:
                
                ( mime, ) = classes
                
                if mime == HC.APPLICATION_HYDRUS_CLIENT_COLLECTION:
                    
                    collections_suffix = 's' if num_collections > 1 else ''
                    
                    return 'file{} in {} collection{}'.format( suffix, HydrusData.ToHumanInt( num_collections ), collections_suffix )
                    
                else:
                    
                    return HC.mime_string_lookup[ mime ] + suffix
                    
                
            
            if len( classes.difference( HC.IMAGES ) ) == 0:
                
                return 'image' + suffix
                
            elif len( classes.difference( HC.ANIMATIONS ) ) == 0:
                
                return 'animation' + suffix
                
            elif len( classes.difference( HC.VIDEO ) ) == 0:
                
                return 'video' + suffix
                
            elif len( classes.difference( HC.AUDIO ) ) == 0:
                
                return 'audio file' + suffix
                
            else:
                
                return 'file' + suffix
                
            
        
        if len( self._sorted_media ) > 1000:
            
            sorted_mime_descriptor = 'files'
            
        else:
            
            sorted_mimes = { media.GetMime() for media in self._sorted_media }
            
            if HC.APPLICATION_HYDRUS_CLIENT_COLLECTION in sorted_mimes:
                
                num_collections = len( [ media for media in self._sorted_media if isinstance( media, ClientMedia.MediaCollection ) ] )
                
            else:
                
                num_collections = 0
                
            
            plural = len( self._sorted_media ) > 1 or sum( ( m.GetNumFiles() for m in self._sorted_media ) ) > 1
            
            sorted_mime_descriptor = GetDescriptor( plural, sorted_mimes, num_collections )
            
        
        if len( self._selected_media ) > 1000:
            
            selected_mime_descriptor = 'files'
            
        else:
            
            selected_mimes = { media.GetMime() for media in self._selected_media }
            
            if HC.APPLICATION_HYDRUS_CLIENT_COLLECTION in selected_mimes:
                
                num_collections = len( [ media for media in self._selected_media if isinstance( media, ClientMedia.MediaCollection ) ] )
                
            else:
                
                num_collections = 0
                
            
            plural = len( self._selected_media ) > 1 or sum( ( m.GetNumFiles() for m in self._selected_media ) ) > 1
            
            selected_mime_descriptor = GetDescriptor( plural, selected_mimes, num_collections )
            
        
        return ( sorted_mime_descriptor, selected_mime_descriptor )
        
    
    def _HasFocusSingleton( self ) -> bool:
        
        try:
            
            media = self._GetFocusSingleton()
            
            return True
            
        except HydrusExceptions.DataMissing:
            
            return False
            
        
    
    def _HitMedia( self, media, ctrl, shift ):
        
        if media is None:
            
            if not ctrl and not shift:
                
                self._Select( ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_NONE ) )
                self._SetFocusedMedia( None )
                self._EndShiftSelect()
                
            
        else:
            
            if ctrl and not shift:
                
                if media.IsSelected():
                    
                    self._DeselectSelect( ( media, ), () )
                    
                    if self._focused_media == media:
                        
                        self._SetFocusedMedia( None )
                        
                    
                    self._EndShiftSelect()
                    
                else:
                    
                    self._DeselectSelect( (), ( media, ) )
                    
                    focus_it = False
                    
                    if CG.client_controller.new_options.GetBoolean( 'focus_preview_on_ctrl_click' ):
                        
                        if CG.client_controller.new_options.GetBoolean( 'focus_preview_on_ctrl_click_only_static' ):
                            
                            focus_it = media.GetDurationMS() is None
                            
                        else:
                            
                            focus_it = True
                            
                        
                    
                    if focus_it:
                        
                        self._SetFocusedMedia( media )
                        
                    else:
                        
                        self._last_hit_media = media
                        
                    
                    self._StartShiftSelect( media )
                    
                
            elif shift and self._shift_select_started_with_this_media is not None:
                
                start_index = self._sorted_media.index( self._shift_select_started_with_this_media )
                
                end_index = self._sorted_media.index( media )
                
                if start_index < end_index:
                    
                    media_from_start_of_shift_to_end = set( self._sorted_media[ start_index : end_index + 1 ] )
                    
                else:
                    
                    media_from_start_of_shift_to_end = set( self._sorted_media[ end_index : start_index + 1 ] )
                    
                
                media_to_deselect = [ m for m in self._media_added_in_current_shift_select if m not in media_from_start_of_shift_to_end ]
                media_to_select = [ m for m in media_from_start_of_shift_to_end if not m.IsSelected() ]
                
                self._media_added_in_current_shift_select.difference_update( media_to_deselect )
                self._media_added_in_current_shift_select.update( media_to_select )
                
                self._DeselectSelect( media_to_deselect, media_to_select )
                
                focus_it = False
                
                if CG.client_controller.new_options.GetBoolean( 'focus_preview_on_shift_click' ):
                    
                    if CG.client_controller.new_options.GetBoolean( 'focus_preview_on_shift_click_only_static' ):
                        
                        focus_it = media.GetDurationMS() is None
                        
                    else:
                        
                        focus_it = True
                        
                    
                
                if focus_it:
                    
                    self._SetFocusedMedia( media )
                    
                else:
                    
                    self._last_hit_media = media
                    
                
            else:
                
                if not media.IsSelected():
                    
                    self._DeselectSelect( self._selected_media, ( media, ) )
                    
                else:
                    
                    self._PublishSelectionChange()
                    
                
                self._SetFocusedMedia( media )
                self._StartShiftSelect( media )
                
            
        
    
    def _Inbox( self ):
        
        hashes = self._GetSelectedHashes( discriminant = CC.DISCRIMINANT_ARCHIVE, is_in_file_service_key = CC.COMBINED_LOCAL_FILE_SERVICE_KEY  )
        
        if len( hashes ) > 0:
            
            if HC.options[ 'confirm_archive' ]:
                
                if len( hashes ) > 1:
                    
                    message = 'Send {} files to inbox?'.format( HydrusData.ToHumanInt( len( hashes ) ) )
                    
                    result = ClientGUIDialogsQuick.GetYesNo( self, message )
                    
                    if result != QW.QDialog.Accepted:
                        
                        return
                        
                    
                
            
            CG.client_controller.Write( 'content_updates', ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( CC.COMBINED_LOCAL_FILE_SERVICE_KEY, ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_INBOX, hashes ) ) )
            
        
    
    def _LaunchMediaViewer( self, first_media = None ):
        
        if self._HasFocusSingleton():
            
            media = self._GetFocusSingleton()
            
            if not media.GetLocationsManager().IsLocal():
                
                return
                
            
            new_options = CG.client_controller.new_options
            
            ( media_show_action, media_start_paused, media_start_with_embed ) = new_options.GetMediaShowAction( media.GetMime() )
            
            if media_show_action == CC.MEDIA_VIEWER_ACTION_DO_NOT_SHOW_ON_ACTIVATION_OPEN_EXTERNALLY:
                
                hash = media.GetHash()
                mime = media.GetMime()
                
                client_files_manager = CG.client_controller.client_files_manager
                
                path = client_files_manager.GetFilePath( hash, mime )
                
                new_options = CG.client_controller.new_options
                
                launch_path = new_options.GetMimeLaunch( mime )
                
                HydrusPaths.LaunchFile( path, launch_path )
                
                return
                
            elif media_show_action == CC.MEDIA_VIEWER_ACTION_DO_NOT_SHOW:
                
                return
                
            
        
        media_results = self.GenerateMediaResults( discriminant = CC.DISCRIMINANT_LOCAL, for_media_viewer = True )
        
        if len( media_results ) > 0:
            
            if first_media is None and self._focused_media is not None:
                
                first_media = self._focused_media
                
            
            if first_media is not None:
                
                first_media = first_media.GetDisplayMedia()
                
            
            if first_media is not None and first_media.GetLocationsManager().IsLocal():
                
                first_hash = first_media.GetHash()
                
            else:
                
                first_hash = None
                
            
            self.SetFocusedMedia( None )
            
            canvas_frame = ClientGUICanvasFrame.CanvasFrame( self.window() )
            
            canvas_window = ClientGUICanvas.CanvasMediaListBrowser( canvas_frame, self._page_key, self._location_context, media_results, first_hash )
            
            canvas_frame.SetCanvas( canvas_window )
            
            canvas_window.exitFocusMedia.connect( self.SetFocusedMedia )
            
        
    
    def _ManageNotes( self ):
        
        if self._HasFocusSingleton():
            
            media = self._GetFocusSingleton()
            
            ClientGUIMediaModalActions.EditFileNotes( self, media )
            
            self.setFocus( QC.Qt.OtherFocusReason )
            
        
    
    def _ManageRatings( self ):
        
        flat_media = ClientMedia.FlattenMedia( self._selected_media )
        
        if len( flat_media ) > 0:
            
            if len( CG.client_controller.services_manager.GetServices( HC.RATINGS_SERVICES ) ) > 0:
                
                with ClientGUIDialogsManage.DialogManageRatings( self, flat_media ) as dlg:
                    
                    dlg.exec()
                    
                
                self.setFocus( QC.Qt.OtherFocusReason )
                
            
        
    
    def _ManageTags( self ):
        
        flat_media = ClientMedia.FlattenMedia( self._GetSelectedMediaOrdered() )
        
        if len( flat_media ) > 0:
            
            num_files = self._GetNumSelected()
            
            title = 'manage tags for ' + HydrusData.ToHumanInt( num_files ) + ' files'
            frame_key = 'manage_tags_dialog'
            
            with ClientGUITopLevelWindowsPanels.DialogManage( self, title, frame_key ) as dlg:
                
                panel = ClientGUITags.ManageTagsPanel( dlg, self._location_context, CC.TAG_PRESENTATION_SEARCH_PAGE_MANAGE_TAGS, flat_media )
                
                dlg.SetPanel( panel )
                
                dlg.exec()
                
            
            self.setFocus( QC.Qt.OtherFocusReason )
            
        
    
    def _ManageTimestamps( self ):
        
        ordered_selected_media = self._GetSelectedMediaOrdered()
        
        ordered_selected_flat_media = ClientMedia.FlattenMedia( ordered_selected_media )
        
        if len( ordered_selected_flat_media ) > 0:
            
            ClientGUIMediaModalActions.EditFileTimestamps( self, ordered_selected_flat_media )
            
            self.setFocus( QC.Qt.OtherFocusReason )
            
        
    
    def _ManageURLs( self ):
        
        flat_media = ClientMedia.FlattenMedia( self._selected_media )
        
        if len( flat_media ) > 0:
            
            num_files = self._GetNumSelected()
            
            title = 'manage urls for {} files'.format( num_files )
            
            with ClientGUITopLevelWindowsPanels.DialogManage( self, title ) as dlg:
                
                panel = ClientGUIScrolledPanelsManagement.ManageURLsPanel( dlg, flat_media )
                
                dlg.SetPanel( panel )
                
                dlg.exec()
                
            
            self.setFocus( QC.Qt.OtherFocusReason )
            
        
    
    def _MediaIsVisible( self, media ):
        
        return True
        
    
    def _ModifyUploaders( self, file_service_key ):
        
        hashes = self._GetSelectedHashes()
        
        contents = [ HydrusNetwork.Content( HC.CONTENT_TYPE_FILES, ( hash, ) ) for hash in hashes ]
        
        if len( contents ) > 0:
            
            subject_account_identifiers = [ HydrusNetwork.AccountIdentifier( content = content ) for content in contents ]
            
            frame = ClientGUITopLevelWindowsPanels.FrameThatTakesScrollablePanel( self, 'manage accounts' )
            
            panel = ClientGUIHydrusNetwork.ModifyAccountsPanel( frame, file_service_key, subject_account_identifiers )
            
            frame.SetPanel( panel )
            
        
    
    def _OpenFileInWebBrowser( self ):
        
        if self._HasFocusSingleton():
            
            focused_singleton = self._GetFocusSingleton()
            
            if focused_singleton.GetLocationsManager().IsLocal():
                
                hash = focused_singleton.GetHash()
                mime = focused_singleton.GetMime()
                
                client_files_manager = CG.client_controller.client_files_manager
                
                path = client_files_manager.GetFilePath( hash, mime )
                
                self.focusMediaPaused.emit()
                
                ClientPaths.LaunchPathInWebBrowser( path )
                
            
        
    
    def _MacQuicklook( self ):
        
        if HC.PLATFORM_MACOS and self._HasFocusSingleton():
            
            focused_singleton = self._GetFocusSingleton()
            
            if focused_singleton.GetLocationsManager().IsLocal():
                
                hash = focused_singleton.GetHash()
                mime = focused_singleton.GetMime()
                
                client_files_manager = CG.client_controller.client_files_manager
                
                path = client_files_manager.GetFilePath( hash, mime )
                
                self.focusMediaPaused.emit()
                
                if not MAC_QUARTZ_OK:
                    
                    HydrusData.ShowText( 'Sorry, could not do the Quick Look integration--it looks like your venv does not support it. If you are running from source, try rebuilding it!' )
                    
                
                ClientMacIntegration.show_quicklook_for_path( path )
                
            
        
    
    def _OpenKnownURL( self ):
        
        if self._HasFocusSingleton():
            
            focused_singleton = self._GetFocusSingleton()
            
            ClientGUIMediaModalActions.DoOpenKnownURLFromShortcut( self, focused_singleton )
            
        
    
    def _PetitionFiles( self, remote_service_key ):
        
        hashes = self._GetSelectedHashes()
        
        if hashes is not None and len( hashes ) > 0:
            
            remote_service = CG.client_controller.services_manager.GetService( remote_service_key )
            
            service_type = remote_service.GetServiceType()
            
            if service_type == HC.FILE_REPOSITORY:
                
                if len( hashes ) == 1:
                    
                    message = 'Enter a reason for this file to be removed from {}.'.format( remote_service.GetName() )
                    
                else:
                    
                    message = 'Enter a reason for these {} files to be removed from {}.'.format( HydrusData.ToHumanInt( len( hashes ) ), remote_service.GetName() )
                    
                
                with ClientGUIDialogs.DialogTextEntry( self, message ) as dlg:
                    
                    if dlg.exec() == QW.QDialog.Accepted:
                        
                        reason = dlg.GetValue()
                        
                        content_update = ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_PETITION, hashes, reason = reason )
                        
                        content_update_package = ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( remote_service_key, content_update )
                        
                        CG.client_controller.Write( 'content_updates', content_update_package )
                        
                    
                
                self.setFocus( QC.Qt.OtherFocusReason )
                
            elif service_type == HC.IPFS:
                
                content_update = ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_PETITION, hashes, reason = 'ipfs' )
                
                content_update_package = ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( remote_service_key, content_update )
                
                CG.client_controller.Write( 'content_updates', content_update_package )
                
            
        
    
    def _PublishSelectionChange( self, tags_changed = False ):
        
        if CG.client_controller.gui.IsCurrentPage( self._page_key ):
            
            if len( self._selected_media ) == 0:
                
                tags_media = self._sorted_media
                
            else:
                
                tags_media = self._selected_media
                
            
            tags_media = list( tags_media )
            
            tags_changed = tags_changed or self._had_changes_to_tag_presentation_while_hidden
            
            self.selectedMediaTagPresentationChanged.emit( tags_media, tags_changed )
            
            self.statusTextChanged.emit( self._GetPrettyStatusForStatusBar() )
            
            if tags_changed:
                
                self._had_changes_to_tag_presentation_while_hidden = False
                
            
        elif tags_changed:
            
            self._had_changes_to_tag_presentation_while_hidden = True
            
        
    
    def _PublishSelectionIncrement( self, medias ):
        
        if CG.client_controller.gui.IsCurrentPage( self._page_key ):
            
            medias = list( medias )
            
            self.selectedMediaTagPresentationIncremented.emit( medias )
            
            self.statusTextChanged.emit( self._GetPrettyStatusForStatusBar() )
            
        else:
            
            self._had_changes_to_tag_presentation_while_hidden = True
            
        
    
    def _RecalculateVirtualSize( self, called_from_resize_event = False ):
        
        pass
        
    
    def _RedrawMedia( self, media ):
        
        pass
        
    
    def _Remove( self, file_filter: ClientMediaFileFilter.FileFilter ):
        
        hashes = file_filter.GetMediaListHashes( self )
        
        if len( hashes ) > 0:
            
            self._RemoveMediaByHashes( hashes )
            
        
    
    def _RegenerateFileData( self, job_type ):
        
        flat_media = self._GetSelectedFlatMedia()
        
        num_files = len( flat_media )
        
        if num_files > 0:
            
            if job_type == ClientFiles.REGENERATE_FILE_DATA_JOB_FILE_METADATA:
                
                message = 'This will reparse the {} selected files\' metadata.'.format( HydrusData.ToHumanInt( num_files ) )
                message += '\n' * 2
                message += 'If the files were imported before some more recent improvement in the parsing code (such as EXIF rotation or bad video resolution or duration or frame count calculation), this will update them.'
                
            elif job_type == ClientFiles.REGENERATE_FILE_DATA_JOB_FORCE_THUMBNAIL:
                
                message = 'This will force-regenerate the {} selected files\' thumbnails.'.format( HydrusData.ToHumanInt( num_files ) )
                
            elif job_type == ClientFiles.REGENERATE_FILE_DATA_JOB_REFIT_THUMBNAIL:
                
                message = 'This will regenerate the {} selected files\' thumbnails, but only if they are the wrong size.'.format( HydrusData.ToHumanInt( num_files ) )
                
            else:
                
                message = ClientFiles.regen_file_enum_to_description_lookup[ job_type ]
                
            
            do_it_now = True
            
            if num_files > 50:
                
                message += '\n' * 2
                message += 'You have selected {} files, so this job may take some time. You can run it all now or schedule it to the overall file maintenance queue for later spread-out processing.'.format( HydrusData.ToHumanInt( num_files ) )
                
                yes_tuples = []
                
                yes_tuples.append( ( 'do it now', 'now' ) )
                yes_tuples.append( ( 'do it later', 'later' ) )
                
                try:
                    
                    result = ClientGUIDialogsQuick.GetYesYesNo( self, message, yes_tuples = yes_tuples, no_label = 'forget it' )
                    
                except HydrusExceptions.CancelledException:
                    
                    return
                    
                
                do_it_now = result == 'now'
                
            else:
                
                result = ClientGUIDialogsQuick.GetYesNo( self, message )
                
                if result != QW.QDialog.Accepted:
                    
                    return
                    
                
            
            if do_it_now:
                
                self._SetFocusedMedia( None )
                
                time.sleep( 0.1 )
                
                CG.client_controller.CallToThread( CG.client_controller.files_maintenance_manager.RunJobImmediately, flat_media, job_type )
                
            else:
                
                hashes = { media.GetHash() for media in flat_media }
                
                CG.client_controller.CallToThread( CG.client_controller.files_maintenance_manager.ScheduleJob, hashes, job_type )
                
            
        
    
    def _RescindDownloadSelected( self ):
        
        hashes = self._GetSelectedHashes( discriminant = CC.DISCRIMINANT_NOT_LOCAL )
        
        CG.client_controller.Write( 'content_updates', ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( CC.COMBINED_LOCAL_FILE_SERVICE_KEY, ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_RESCIND_PEND, hashes ) ) )
        
    
    def _RescindPetitionFiles( self, file_service_key ):
        
        hashes = self._GetSelectedHashes()
        
        if hashes is not None and len( hashes ) > 0:   
            
            CG.client_controller.Write( 'content_updates', ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( file_service_key, ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_RESCIND_PETITION, hashes ) ) )
            
        
    
    def _RescindUploadFiles( self, file_service_key ):
        
        hashes = self._GetSelectedHashes()
        
        if hashes is not None and len( hashes ) > 0:   
            
            CG.client_controller.Write( 'content_updates', ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( file_service_key, ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_RESCIND_PEND, hashes ) ) )
            
        
    
    def _Select( self, file_filter: ClientMediaFileFilter.FileFilter ):
        
        matching_media = file_filter.GetMediaListMedia( self )
        
        media_to_deselect = self._selected_media.difference( matching_media )
        media_to_select = matching_media.difference( self._selected_media )
        
        move_focus = self._focused_media in media_to_deselect or self._focused_media is None
        
        if move_focus or self._shift_select_started_with_this_media in media_to_deselect:
            
            self._EndShiftSelect()
            
        
        self._DeselectSelect( media_to_deselect, media_to_select )
        
        if move_focus:
            
            if len( self._selected_media ) == 0:
                
                self._SetFocusedMedia( None )
                
            else:
                
                # let's not focus if one of the selectees is already visible
                
                media_visible = True in ( self._MediaIsVisible( media ) for media in self._selected_media )
                
                if not media_visible:
                    
                    for m in self._sorted_media:
                        
                        if m in self._selected_media:
                            
                            ctrl = False
                            shift = False
                            
                            self._HitMedia( m, ctrl, shift )
                            
                            self._ScrollToMedia( m )
                            
                            break
                            
                        
                    
                
            
        
    
    def _SetCollectionsAsAlternate( self ):
        
        collections = self._GetSelectedCollections()
        
        if len( collections ) > 0:
            
            message = 'Are you sure you want to set files in the selected collections as alternates? Each collection will be considered a separate group of alternates.'
            message += '\n' * 2
            message += 'Be careful applying this to large groups--any more than a few dozen files, and the client could hang a long time.'
            
            result = ClientGUIDialogsQuick.GetYesNo( self, message )
            
            if result == QW.QDialog.Accepted:
                
                for collection in collections:
                    
                    media_group = collection.GetFlatMedia()
                    
                    self._SetDuplicates( HC.DUPLICATE_ALTERNATE, media_group = media_group, silent = True )
                    
                
            
        
    
    def _SetDuplicates( self, duplicate_type, media_pairs = None, media_group = None, duplicate_content_merge_options = None, silent = False ):
        
        if duplicate_type == HC.DUPLICATE_POTENTIAL:
            
            yes_no_text = 'queue all possible and valid pair combinations into the duplicate filter'
            
        elif duplicate_content_merge_options is None:
            
            yes_no_text = 'apply "{}"'.format( HC.duplicate_type_string_lookup[ duplicate_type ] )
            
            if duplicate_type in [ HC.DUPLICATE_BETTER, HC.DUPLICATE_SAME_QUALITY ] or ( CG.client_controller.new_options.GetBoolean( 'advanced_mode' ) and duplicate_type == HC.DUPLICATE_ALTERNATE ):
                
                yes_no_text += ' (with default duplicate metadata merge options)'
                
                new_options = CG.client_controller.new_options
                
                duplicate_content_merge_options = new_options.GetDuplicateContentMergeOptions( duplicate_type )
                
            
        else:
            
            yes_no_text = 'apply "{}" (with custom duplicate metadata merge options)'.format( HC.duplicate_type_string_lookup[ duplicate_type ] )
            
        
        file_deletion_reason = 'Deleted from duplicate action on Media Page ({}).'.format( yes_no_text )
        
        if media_pairs is None:
            
            if media_group is None:
                
                flat_media = self._GetSelectedFlatMedia()
                
            else:
                
                flat_media = ClientMedia.FlattenMedia( media_group )
                
            
            num_files_str = HydrusData.ToHumanInt( len( flat_media ) )
            
            if len( flat_media ) < 2:
                
                return False
                
            
            if duplicate_type in ( HC.DUPLICATE_FALSE_POSITIVE, HC.DUPLICATE_ALTERNATE, HC.DUPLICATE_POTENTIAL ):
                
                media_pairs = list( itertools.combinations( flat_media, 2 ) )
                
            else:
                
                first_media = flat_media[0]
                
                media_pairs = [ ( first_media, other_media ) for other_media in flat_media if other_media != first_media ]
                
            
        else:
            
            num_files_str = HydrusData.ToHumanInt( len( self._GetSelectedFlatMedia() ) )
            
        
        if len( media_pairs ) == 0:
            
            return False
            
        
        if not silent:
            
            yes_label = 'yes'
            no_label = 'no'
            
            if len( media_pairs ) > 1 and duplicate_type in ( HC.DUPLICATE_FALSE_POSITIVE, HC.DUPLICATE_ALTERNATE ):
                
                media_pairs_str = HydrusData.ToHumanInt( len( media_pairs ) )
                
                message = 'Are you sure you want to {} for the {} selected files? The relationship will be applied between every pair combination in the file selection ({} pairs).'.format( yes_no_text, num_files_str, media_pairs_str )
                
                if len( media_pairs ) > 100:
                    
                    if duplicate_type == HC.DUPLICATE_FALSE_POSITIVE:
                        
                        message = 'False positive records are complicated, and setting that relationship for {} files ({} pairs) at once is likely a mistake.'.format( num_files_str, media_pairs_str )
                        message += '\n' * 2
                        message += 'Are you sure all of these files are all potential duplicates and that they are all false positive matches with each other? If not, I recommend you step back for now.'
                        
                        yes_label = 'I know what I am doing'
                        no_label = 'step back for now'
                        
                    elif duplicate_type == HC.DUPLICATE_ALTERNATE:
                        
                        message = 'Are you certain all these {} files are alternates with every other member of the selection, and that none are duplicates?'.format( num_files_str )
                        message += '\n' * 2
                        message += 'If some of them may be duplicates, I recommend you either deselect the possible duplicates and try again, or just leave this group to be processed in the normal duplicate filter.'
                        
                        yes_label = 'they are all alternates'
                        no_label = 'some may be duplicates'
                        
                    
                
            else:
                
                message = 'Are you sure you want to ' + yes_no_text + ' for the {} selected files?'.format( num_files_str )
                
            
            result = ClientGUIDialogsQuick.GetYesNo( self, message, yes_label = yes_label, no_label = no_label )
            
            if result != QW.QDialog.Accepted:
                
                return False
                
            
        
        pair_info = []
        
        # there's an issue here in that one decision will affect the next. if we say 'copy tags both sides' and say A > B & C, then B's tags, merged with A, should soon merge with C
        # therefore, we need to update the media objects as we go here, which means we need duplicates to force content updates on
        # this is a little hacky, so maybe a big rewrite here would be nice
        
        # There's a second issue, wew, in that in order to propagate C back to B, we need to do the whole thing twice! wow!
        # some service_key_to_content_updates preservation gubbins is needed as a result
        
        hashes_to_duplicated_media = {}
        hash_pairs_to_content_update_packages = collections.defaultdict( list )
        
        for is_first_run in ( True, False ):
            
            for ( first_media, second_media ) in media_pairs:
                
                first_hash = first_media.GetHash()
                second_hash = second_media.GetHash()
                
                if first_hash not in hashes_to_duplicated_media:
                    
                    hashes_to_duplicated_media[ first_hash ] = first_media.Duplicate()
                    
                
                first_duplicated_media = hashes_to_duplicated_media[ first_hash ]
                
                if second_hash not in hashes_to_duplicated_media:
                    
                    hashes_to_duplicated_media[ second_hash ] = second_media.Duplicate()
                    
                
                second_duplicated_media = hashes_to_duplicated_media[ second_hash ]
                
                content_update_packages = hash_pairs_to_content_update_packages[ ( first_hash, second_hash ) ]
                
                if duplicate_content_merge_options is not None:
                    
                    do_not_do_deletes = is_first_run
                    
                    # so the important part of this mess is here. we send the duplicated media, which is keeping up with content updates, to the method here
                    # original 'first_media' is not changed, and won't be until the database Write clears and publishes everything
                    content_update_packages.append( duplicate_content_merge_options.ProcessPairIntoContentUpdatePackage( first_duplicated_media, second_duplicated_media, file_deletion_reason = file_deletion_reason, do_not_do_deletes = do_not_do_deletes ) )
                    
                
                for content_update_package in content_update_packages:
                    
                    for ( service_key, content_updates ) in content_update_package.IterateContentUpdates():
                        
                        for content_update in content_updates:
                            
                            hashes = content_update.GetHashes()
                            
                            if first_hash in hashes:
                                
                                first_duplicated_media.GetMediaResult().ProcessContentUpdate( service_key, content_update )
                                
                            
                            if second_hash in hashes:
                                
                                second_duplicated_media.GetMediaResult().ProcessContentUpdate( service_key, content_update )
                                
                            
                        
                    
                
                if is_first_run:
                    
                    continue
                    
                
                pair_info.append( ( duplicate_type, first_hash, second_hash, content_update_packages ) )
                
            
        
        if len( pair_info ) > 0:
            
            CG.client_controller.WriteSynchronous( 'duplicate_pair_status', pair_info )
            
            return True
            
        
        return False
        
    
    def _SetDuplicatesCustom( self ):
        
        duplicate_types = [ HC.DUPLICATE_BETTER, HC.DUPLICATE_SAME_QUALITY ]
        
        if CG.client_controller.new_options.GetBoolean( 'advanced_mode' ):
            
            duplicate_types.append( HC.DUPLICATE_ALTERNATE )
            
        
        choice_tuples = [ ( HC.duplicate_type_string_lookup[ duplicate_type ], duplicate_type ) for duplicate_type in duplicate_types ]
        
        try:
            
            duplicate_type = ClientGUIDialogsQuick.SelectFromList( self, 'select duplicate type', choice_tuples )
            
        except HydrusExceptions.CancelledException:
            
            return
            
        
        new_options = CG.client_controller.new_options
        
        duplicate_content_merge_options = new_options.GetDuplicateContentMergeOptions( duplicate_type )
        
        with ClientGUITopLevelWindowsPanels.DialogEdit( self, 'edit duplicate merge options' ) as dlg:
            
            panel = ClientGUIScrolledPanelsEdit.EditDuplicateContentMergeOptionsPanel( dlg, duplicate_type, duplicate_content_merge_options, for_custom_action = True )
            
            dlg.SetPanel( panel )
            
            if dlg.exec() == QW.QDialog.Accepted:
                
                duplicate_content_merge_options = panel.GetValue()
                
                if duplicate_type == HC.DUPLICATE_BETTER:
                    
                    self._SetDuplicatesFocusedBetter( duplicate_content_merge_options = duplicate_content_merge_options )
                    
                else:
                    
                    self._SetDuplicates( duplicate_type, duplicate_content_merge_options = duplicate_content_merge_options )
                    
                
            
        
    
    def _SetDuplicatesFocusedBetter( self, duplicate_content_merge_options = None ):
        
        if self._HasFocusSingleton():
            
            focused_singleton = self._GetFocusSingleton()
            
            focused_hash = focused_singleton.GetHash()
            
            flat_media = self._GetSelectedFlatMedia()
            
            ( better_media, ) = [ media for media in flat_media if media.GetHash() == focused_hash ]
            
            worse_flat_media = [ media for media in flat_media if media.GetHash() != focused_hash ]
            
            if len( worse_flat_media ) == 0:
                
                message = 'Since you only selected one file, would you rather just set this file as the best file of its group?'
                
                result = ClientGUIDialogsQuick.GetYesNo( self, message )
                
                if result == QW.QDialog.Accepted:
                    
                    self._SetDuplicatesFocusedKing( silent = True )
                    
                
                return
                
            
            media_pairs = [ ( better_media, worse_media ) for worse_media in worse_flat_media ]
            
            message = 'Are you sure you want to set the focused file as better than the {} other files in the selection?'.format( HydrusData.ToHumanInt( len( worse_flat_media ) ) )
            
            result = ClientGUIDialogsQuick.GetYesNo( self, message )
            
            if result == QW.QDialog.Accepted:
                
                self._SetDuplicates( HC.DUPLICATE_BETTER, media_pairs = media_pairs, silent = True, duplicate_content_merge_options = duplicate_content_merge_options )
                
            
        else:
            
            ClientGUIDialogsMessage.ShowWarning( self, 'No file is focused, so cannot set the focused file as better!' )
            
            return
            
        
    
    def _SetDuplicatesFocusedKing( self, silent = False ):
        
        if self._HasFocusSingleton():
            
            media = self._GetFocusSingleton()
            
            focused_hash = media.GetHash()
            
            # TODO: when media knows its duplicate gubbins, we can test num dupe files and if it is king already and stuff easier here
            
            do_it = False
            
            if silent:
                
                do_it = True
                
            else:
                
                message = 'Are you sure you want to set the focused file as the best file of its duplicate group?'
                
                result = ClientGUIDialogsQuick.GetYesNo( self, message )
                
                if result == QW.QDialog.Accepted:
                    
                    do_it = True
                    
                
            
            if do_it:
                
                CG.client_controller.WriteSynchronous( 'duplicate_set_king', focused_hash )
                
            
        else:
            
            ClientGUIDialogsMessage.ShowWarning( self, 'No file is focused, so cannot set the focused file as king!' )
            
            return
            
        
    
    def _SetDuplicatesPotential( self ):
        
        media_group = self._GetSelectedFlatMedia()
        
        self._SetDuplicates( HC.DUPLICATE_POTENTIAL, media_group = media_group )
        
    
    def _SetFocusedMedia( self, media ):
        
        if media is None and self._focused_media is not None:
            
            next_best_media = self._focused_media
            
            i = self._sorted_media.index( next_best_media )
            
            while next_best_media in self._selected_media:
                
                if i == 0:
                    
                    next_best_media = None
                    
                    break
                    
                
                i -= 1
                
                next_best_media = self._sorted_media[ i ]
                
            
            self._next_best_media_if_focuses_removed = next_best_media
            
        else:
            
            self._next_best_media_if_focuses_removed = None
            
        
        publish_media = None
        
        self._focused_media = media
        self._last_hit_media = media
        
        if self._focused_media is not None:
            
            publish_media = self._focused_media.GetDisplayMedia()
            
        
        if publish_media is None:
            
            self.focusMediaCleared.emit()
            
        else:
            
            self.focusMediaChanged.emit( publish_media )
            
        
    
    def _ScrollToMedia( self, media ):
        
        pass
        
    
    def _ShowSelectionInNewPage( self ):
        
        hashes = self._GetSelectedHashes( ordered = True )
        
        if len( hashes ) > 0:
            
            media_sort = self._management_controller.GetVariable( 'media_sort' )
            
            if self._management_controller.HasVariable( 'media_collect' ):
                
                media_collect = self._management_controller.GetVariable( 'media_collect' )
                
            else:
                
                media_collect = ClientMedia.MediaCollect()
                
            
            ClientGUIMediaSimpleActions.ShowFilesInNewPage( hashes, self._location_context, media_sort = media_sort, media_collect = media_collect )
            
        
    
    def _StartShiftSelect( self, media ):
        
        self._shift_select_started_with_this_media = media
        self._media_added_in_current_shift_select = set()
        
    
    def _Undelete( self ):
        
        media = self._GetSelectedFlatMedia()
        
        ClientGUIMediaModalActions.UndeleteMedia( self, media )
        
    
    def _UpdateBackgroundColour( self ):
        
        self.widget().update()
        
    
    def _UploadDirectory( self, file_service_key ):
        
        hashes = self._GetSelectedHashes()
        
        if hashes is not None and len( hashes ) > 0:
            
            ipfs_service = CG.client_controller.services_manager.GetService( file_service_key )
            
        
        with ClientGUIDialogs.DialogTextEntry( self, 'Enter a note to describe this directory.' ) as dlg:
            
            if dlg.exec() == QW.QDialog.Accepted:
                
                note = dlg.GetValue()
                
                CG.client_controller.CallToThread( ipfs_service.PinDirectory, hashes, note )
                
            
        
    
    def _UploadFiles( self, file_service_key ):
        
        hashes = self._GetSelectedHashes( is_not_in_file_service_key = file_service_key )
        
        if hashes is not None and len( hashes ) > 0:   
            
            CG.client_controller.Write( 'content_updates', ClientContentUpdates.ContentUpdatePackage.STATICCreateFromContentUpdate( file_service_key, ClientContentUpdates.ContentUpdate( HC.CONTENT_TYPE_FILES, HC.CONTENT_UPDATE_PEND, hashes ) ) )
            
        
    
    def AddMediaResults( self, page_key, media_results ):
        
        if page_key == self._page_key:
            
            CG.client_controller.pub( 'refresh_page_name', self._page_key )
            
            result = ClientMedia.ListeningMediaList.AddMediaResults( self, media_results )
            
            self.newMediaAdded.emit()
            
            CG.client_controller.pub( 'notify_new_pages_count' )
            
            return result
            
        
    
    def CleanBeforeDestroy( self ):
        
        self.Clear()
        
    
    def ClearPageKey( self ):
        
        self._page_key = b'dead media panel page key'
        
    
    def Collect( self, media_collect = None ):
        
        self._Select( ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_NONE ) )
        
        ClientMedia.ListeningMediaList.Collect( self, media_collect = media_collect )
        
        self._RecalculateVirtualSize()
        
        self.Sort()
        
    
    def GetTotalFileSize( self ):
        
        return 0
        
    
    def LaunchMediaViewerOnFocus( self, page_key ):
        
        if page_key == self._page_key:
            
            self._LaunchMediaViewer()
            
        
    
    def PageHidden( self ):
        
        pass
        
    
    def PageShown( self ):
        
        self._PublishSelectionChange()
        
    
    def ProcessApplicationCommand( self, command: CAC.ApplicationCommand ):
        
        command_processed = True
        
        if command.IsSimpleCommand():
            
            action = command.GetSimpleAction()
            
            if action == CAC.SIMPLE_COPY_FILE_BITMAP:
                
                if not self._HasFocusSingleton():
                    
                    return
                    
                
                focus_singleton = self._GetFocusSingleton()
                
                bitmap_type = command.GetSimpleData()
                
                ClientGUIMediaSimpleActions.CopyMediaBitmap( focus_singleton, bitmap_type )
                
            elif action == CAC.SIMPLE_COPY_FILES:
                
                file_command_target = command.GetSimpleData()
                
                medias = self._GetMediasForFileCommandTarget( file_command_target )
                
                if len( medias ) > 0:
                    
                    ClientGUIMediaSimpleActions.CopyFilesToClipboard( medias )
                    
                
            elif action == CAC.SIMPLE_COPY_FILE_PATHS:
                
                file_command_target = command.GetSimpleData()
                
                medias = self._GetMediasForFileCommandTarget( file_command_target )
                
                if len( medias ) > 0:
                    
                    ClientGUIMediaSimpleActions.CopyFilePathsToClipboard( medias )
                    
                
            elif action == CAC.SIMPLE_COPY_FILE_HASHES:
                
                ( file_command_target, hash_type ) = command.GetSimpleData()
                
                medias = self._GetMediasForFileCommandTarget( file_command_target )
                
                if len( medias ) > 0:
                    
                    ClientGUIMediaModalActions.CopyHashesToClipboard( self, hash_type, medias )
                    
                
            elif action == CAC.SIMPLE_COPY_FILE_SERVICE_FILENAMES:
                
                hacky_ipfs_dict = command.GetSimpleData()
                
                file_command_target = hacky_ipfs_dict[ 'file_command_target' ]
                ipfs_service_key = hacky_ipfs_dict[ 'ipfs_service_key' ]
                
                medias = self._GetMediasForFileCommandTarget( file_command_target )
                
                if len( medias ) > 0:
                    
                    ClientGUIMediaSimpleActions.CopyServiceFilenamesToClipboard( ipfs_service_key, medias )
                    
                
            elif action == CAC.SIMPLE_COPY_FILE_ID:
                
                file_command_target = command.GetSimpleData()
                
                medias = self._GetMediasForFileCommandTarget( file_command_target )
                
                if len( medias ) > 0:
                    
                    ClientGUIMediaSimpleActions.CopyFileIdsToClipboard( medias )
                    
                
            elif action == CAC.SIMPLE_COPY_URLS:
                
                ordered_selected_media = self._GetSelectedMediaOrdered()
                
                if len( ordered_selected_media ) > 0:
                    
                    ClientGUIMediaSimpleActions.CopyMediaURLs( ordered_selected_media )
                    
                
            elif action == CAC.SIMPLE_REARRANGE_THUMBNAILS:
                
                ordered_selected_media = self._GetSelectedMediaOrdered()
                
                ( rearrange_type, rearrange_data ) = command.GetSimpleData()
                
                insertion_index = None
                
                if rearrange_type == CAC.REARRANGE_THUMBNAILS_TYPE_FIXED:
                    
                    insertion_index = rearrange_data
                    
                elif rearrange_type == CAC.REARRANGE_THUMBNAILS_TYPE_COMMAND:
                    
                    rearrange_command = rearrange_data
                    
                    if rearrange_command == CAC.MOVE_HOME:
                        
                        insertion_index = 0
                        
                    elif rearrange_command == CAC.MOVE_END:
                        
                        insertion_index = len( self._sorted_media )
                        
                    else:
                        
                        if len( self._selected_media ) > 0:
                            
                            if rearrange_command in ( CAC.MOVE_LEFT, CAC.MOVE_RIGHT ):
                                
                                ordered_selected_media = self._GetSelectedMediaOrdered()
                                
                                earliest_index = self._sorted_media.index( ordered_selected_media[0] )
                                
                                if rearrange_command == CAC.MOVE_LEFT:
                                    
                                    if earliest_index > 0:
                                        
                                        insertion_index = earliest_index - 1
                                        
                                    
                                elif rearrange_command == CAC.MOVE_RIGHT:
                                    
                                    insertion_index = earliest_index + 1
                                    
                                
                            elif rearrange_command == CAC.MOVE_TO_FOCUS:
                                
                                if self._focused_media is not None:
                                    
                                    focus_index = self._sorted_media.index( self._focused_media )
                                    
                                    insertion_index = focus_index
                                    
                                
                            
                        
                    
                
                if insertion_index is None:
                    
                    return
                    
                
                self.MoveMedia( ordered_selected_media, insertion_index = insertion_index )
                
            elif action == CAC.SIMPLE_SHOW_DUPLICATES:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    duplicate_type = command.GetSimpleData()
                    
                    ClientGUIMediaSimpleActions.ShowDuplicatesInNewPage( self._location_context, hash, duplicate_type )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_CLEAR_FOCUSED_FALSE_POSITIVES:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.ClearFalsePositives( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_CLEAR_FALSE_POSITIVES:
                
                hashes = self._GetSelectedHashes()
                
                if len( hashes ) > 0:
                    
                    ClientGUIDuplicates.ClearFalsePositives( self, hashes )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_DISSOLVE_FOCUSED_ALTERNATE_GROUP:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.DissolveAlternateGroup( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_DISSOLVE_ALTERNATE_GROUP:
                
                hashes = self._GetSelectedHashes()
                
                if len( hashes ) > 0:
                    
                    ClientGUIDuplicates.DissolveAlternateGroup( self, hashes )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_DISSOLVE_FOCUSED_DUPLICATE_GROUP:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.DissolveDuplicateGroup( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_DISSOLVE_DUPLICATE_GROUP:
                
                hashes = self._GetSelectedHashes()
                
                if len( hashes ) > 0:
                    
                    ClientGUIDuplicates.DissolveDuplicateGroup( self, hashes )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_REMOVE_FOCUSED_FROM_ALTERNATE_GROUP:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.RemoveFromAlternateGroup( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_REMOVE_FOCUSED_FROM_DUPLICATE_GROUP:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.RemoveFromDuplicateGroup( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_RESET_FOCUSED_POTENTIAL_SEARCH:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.ResetPotentialSearch( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_RESET_POTENTIAL_SEARCH:
                
                hashes = self._GetSelectedHashes()
                
                if len( hashes ) > 0:
                    
                    ClientGUIDuplicates.ResetPotentialSearch( self, hashes )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_REMOVE_FOCUSED_POTENTIALS:
                
                if self._HasFocusSingleton():
                    
                    media = self._GetFocusSingleton()
                    
                    hash = media.GetHash()
                    
                    ClientGUIDuplicates.RemovePotentials( self, ( hash, ) )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_REMOVE_POTENTIALS:
                
                hashes = self._GetSelectedHashes()
                
                if len( hashes ) > 0:
                    
                    ClientGUIDuplicates.RemovePotentials( self, hashes )
                    
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_ALTERNATE:
                
                self._SetDuplicates( HC.DUPLICATE_ALTERNATE )
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_ALTERNATE_COLLECTIONS:
                
                self._SetCollectionsAsAlternate()
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_CUSTOM:
                
                self._SetDuplicatesCustom()
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_FOCUSED_BETTER:
                
                self._SetDuplicatesFocusedBetter()
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_FOCUSED_KING:
                
                self._SetDuplicatesFocusedKing()
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_POTENTIAL:
                
                self._SetDuplicatesPotential()
                
            elif action == CAC.SIMPLE_DUPLICATE_MEDIA_SET_SAME_QUALITY:
                
                self._SetDuplicates( HC.DUPLICATE_SAME_QUALITY )
                
            elif action in ( CAC.SIMPLE_EXPORT_FILES, CAC.SIMPLE_EXPORT_FILES_QUICK_AUTO_EXPORT ):
                
                do_export_and_then_quit = action == CAC.SIMPLE_EXPORT_FILES_QUICK_AUTO_EXPORT
                
                if len( self._selected_media ) > 0:
                    
                    medias = self._GetSelectedMediaOrdered()
                    
                    flat_media = ClientMedia.FlattenMedia( medias )
                    
                    ClientGUIMediaModalActions.ExportFiles( self, flat_media, do_export_and_then_quit = do_export_and_then_quit )
                    
                
            elif action == CAC.SIMPLE_MANAGE_FILE_RATINGS:
                
                self._ManageRatings()
                
            elif action == CAC.SIMPLE_MANAGE_FILE_TAGS:
                
                self._ManageTags()
                
            elif action == CAC.SIMPLE_MANAGE_FILE_URLS:
                
                self._ManageURLs()
                
            elif action == CAC.SIMPLE_MANAGE_FILE_NOTES:
                
                self._ManageNotes()
                
            elif action == CAC.SIMPLE_MANAGE_FILE_TIMESTAMPS:
                
                self._ManageTimestamps()
                
            elif action == CAC.SIMPLE_OPEN_KNOWN_URL:
                
                self._OpenKnownURL()
                
            elif action == CAC.SIMPLE_ARCHIVE_FILE:
                
                self._Archive()
                
            elif action == CAC.SIMPLE_DELETE_FILE:
                
                self._Delete()
                
            elif action == CAC.SIMPLE_UNDELETE_FILE:
                
                self._Undelete()
                
            elif action == CAC.SIMPLE_INBOX_FILE:
                
                self._Inbox()
                
            elif action == CAC.SIMPLE_REMOVE_FILE_FROM_VIEW:
                
                self._Remove( ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_SELECTED ) )
                
            elif action == CAC.SIMPLE_LAUNCH_MEDIA_VIEWER:
                
                self._LaunchMediaViewer()
                
            elif action == CAC.SIMPLE_OPEN_FILE_IN_EXTERNAL_PROGRAM:
                
                if self._HasFocusSingleton():
                    
                    focused_singleton = self._GetFocusSingleton()
                    
                    it_worked = ClientGUIMediaSimpleActions.OpenExternally( focused_singleton )
                    
                    if it_worked:
                        
                        self.focusMediaPaused.emit()
                        
                    
                
            elif action == CAC.SIMPLE_OPEN_FILE_IN_FILE_EXPLORER:
                
                if self._HasFocusSingleton():
                    
                    focused_singleton = self._GetFocusSingleton()
                    
                    it_worked = ClientGUIMediaSimpleActions.OpenFileLocation( focused_singleton )
                    
                    if it_worked:
                        
                        self.focusMediaPaused.emit()
                        
                    
                
            elif action == CAC.SIMPLE_OPEN_FILE_IN_WEB_BROWSER:
                
                if self._HasFocusSingleton():
                    
                    focused_singleton = self._GetFocusSingleton()
                    
                    it_worked = ClientGUIMediaSimpleActions.OpenInWebBrowser( focused_singleton )
                    
                    if it_worked:
                        
                        self.focusMediaPaused.emit()
                        
                    
                
            elif action == CAC.SIMPLE_OPEN_SELECTION_IN_NEW_PAGE:
                
                self._ShowSelectionInNewPage()
                
            elif action == CAC.SIMPLE_OPEN_SELECTION_IN_NEW_DUPLICATES_FILTER_PAGE:
                
                hashes = self._GetSelectedHashes( ordered = True )
                
                ClientGUIMediaSimpleActions.ShowFilesInNewDuplicatesFilterPage( hashes, self._location_context )
                
            elif action == CAC.SIMPLE_OPEN_SIMILAR_LOOKING_FILES:
                
                media = self._GetSelectedFlatMedia()
                
                hamming_distance = command.GetSimpleData()
                
                ClientGUIMediaSimpleActions.ShowSimilarFilesInNewPage( media, self._location_context, hamming_distance )
                
            elif action == CAC.SIMPLE_LAUNCH_THE_ARCHIVE_DELETE_FILTER:
                
                self._ArchiveDeleteFilter()
                
            elif action == CAC.SIMPLE_MAC_QUICKLOOK:
                
                self._MacQuicklook()
                
            else:
                
                command_processed = False
                
            
        elif command.IsContentCommand():
            
            command_processed = ClientGUIMediaModalActions.ApplyContentApplicationCommandToMedia( self, command, self._GetSelectedFlatMedia() )
            
        else:
            
            command_processed = False
            
        
        return command_processed
        
    
    def ProcessContentUpdatePackage( self, content_update_package: ClientContentUpdates.ContentUpdatePackage ):
        
        ClientMedia.ListeningMediaList.ProcessContentUpdatePackage( self, content_update_package )
        
        we_were_file_or_tag_affected = False
        
        for ( service_key, content_updates ) in content_update_package.IterateContentUpdates():
            
            for content_update in content_updates:
                
                hashes = content_update.GetHashes()
                
                if self._HasHashes( hashes ):
                    
                    affected_media = self._GetMedia( hashes )
                    
                    self._RedrawMedia( affected_media )
                    
                    if content_update.GetDataType() in ( HC.CONTENT_TYPE_FILES, HC.CONTENT_TYPE_MAPPINGS ):
                        
                        we_were_file_or_tag_affected = True
                        
                    
                
            
        
        if we_were_file_or_tag_affected:
            
            self._PublishSelectionChange( tags_changed = True )
            
        
    
    def ProcessServiceUpdates( self, service_keys_to_service_updates ):
        
        ClientMedia.ListeningMediaList.ProcessServiceUpdates( self, service_keys_to_service_updates )
        
        for ( service_key, service_updates ) in list(service_keys_to_service_updates.items()):
            
            for service_update in service_updates:
                
                ( action, row ) = service_update.ToTuple()
                
                if action in ( HC.SERVICE_UPDATE_DELETE_PENDING, HC.SERVICE_UPDATE_RESET ):
                    
                    self._RecalculateVirtualSize()
                    
                
                self._PublishSelectionChange( tags_changed = True )
                
            
        
    
    def PublishSelectionChange( self ):
        
        self._PublishSelectionChange()
        
    
    def RemoveMedia( self, page_key, hashes ):
        
        if page_key == self._page_key:
            
            self._RemoveMediaByHashes( hashes )
            
        
    
    def SelectByTags( self, page_key, tag_service_key, and_or_or, tags ):
        
        if page_key == self._page_key:
            
            self._Select( ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_TAGS, ( tag_service_key, and_or_or, tags ) ) )
            
            self.setFocus( QC.Qt.OtherFocusReason )
            
        
    
    def SetDuplicateStatusForAll( self, duplicate_type ):
        
        media_group = ClientMedia.FlattenMedia( self._sorted_media )
        
        return self._SetDuplicates( duplicate_type, media_group = media_group )
        
    
    def SetEmptyPageStatusOverride( self, value: str ):
        
        self._empty_page_status_override = value
        
    
    def SetFocusedMedia( self, media ):
        
        pass
        
    
    class _InnerWidget( QW.QWidget ):
        
        def __init__( self, parent ):
            
            QW.QWidget.__init__( self, parent )
            
            self._parent = parent
            
        
        def paintEvent( self, event ):
            
            painter = QG.QPainter( self )
            
            bg_colour = CG.client_controller.new_options.GetColour( CC.COLOUR_THUMBGRID_BACKGROUND )
            
            painter.setBackground( QG.QBrush( bg_colour ) )
            
            painter.eraseRect( painter.viewport() )
            
            background_pixmap = CG.client_controller.bitmap_manager.GetMediaBackgroundPixmap()
            
            if background_pixmap is not None:
                
                my_size = QP.ScrollAreaVisibleRect( self._parent ).size()
                
                pixmap_size = background_pixmap.size()
                
                painter.drawPixmap( my_size.width() - pixmap_size.width(), my_size.height() - pixmap_size.height(), background_pixmap )
                
            
        
    
class MediaPanelLoading( MediaPanel ):
    
    def __init__( self, parent, page_key, management_controller: ClientGUIManagementController.ManagementController ):
        
        self._current = None
        self._max = None
        
        MediaPanel.__init__( self, parent, page_key, management_controller, [] )
        
        CG.client_controller.sub( self, 'SetNumQueryResults', 'set_num_query_results' )
        
    
    def _GetPrettyStatusForStatusBar( self ):
        
        s = 'Loading' + HC.UNICODE_ELLIPSIS
        
        if self._current is not None:
            
            s += ' ' + HydrusData.ToHumanInt( self._current )
            
            if self._max is not None:
                
                s += ' of ' + HydrusData.ToHumanInt( self._max )
                
            
        
        return s
        
    
    def GetSortedMedia( self ):
        
        return []
        
    
    def SetNumQueryResults( self, page_key, num_current, num_max ):
        
        if page_key == self._page_key:
            
            self._current = num_current
            
            self._max = num_max
            
            self._PublishSelectionChange()
            
        
    
class MediaPanelThumbnails( MediaPanel ):
    
    def __init__( self, parent, page_key, management_controller: ClientGUIManagementController.ManagementController, media_results ):
        
        self._clean_canvas_pages = {}
        self._dirty_canvas_pages = []
        self._num_rows_per_canvas_page = 1
        self._num_rows_per_actual_page = 1
        
        self._last_size = QC.QSize( 20, 20 )
        self._num_columns = 1
        
        self._drag_init_coordinates = None
        self._drag_click_timestamp_ms = 0
        self._drag_prefire_event_count = 0
        self._hashes_to_thumbnails_waiting_to_be_drawn: typing.Dict[ bytes, ThumbnailWaitingToBeDrawn ] = {}
        self._hashes_faded = set()
        
        MediaPanel.__init__( self, parent, page_key, management_controller, media_results )
        
        self._last_device_pixel_ratio = self.devicePixelRatio()
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        thumbnail_scroll_rate = float( CG.client_controller.new_options.GetString( 'thumbnail_scroll_rate' ) )
        
        self.verticalScrollBar().setSingleStep( int( round( thumbnail_span_height * thumbnail_scroll_rate ) ) )
        
        self._widget_event_filter = QP.WidgetEventFilter( self.widget() )
        self._widget_event_filter.EVT_LEFT_DCLICK( self.EventMouseFullScreen )
        self._widget_event_filter.EVT_MIDDLE_DOWN( self.EventMouseFullScreen )
        
        # notice this is on widget, not myself. fails to set up scrollbars if just moved up
        # there's a job in qt to-do to sort all this out and fix other scroll issues
        self._widget_event_filter.EVT_SIZE( self.EventResize )
        
        self.widget().setMinimumSize( 50, 50 )
        
        self._UpdateScrollBars()
        
        CG.client_controller.sub( self, 'MaintainPageCache', 'memory_maintenance_pulse' )
        CG.client_controller.sub( self, 'NotifyNewFileInfo', 'new_file_info' )
        CG.client_controller.sub( self, 'NewThumbnails', 'new_thumbnails' )
        CG.client_controller.sub( self, 'ThumbnailsReset', 'notify_complete_thumbnail_reset' )
        CG.client_controller.sub( self, 'RedrawAllThumbnails', 'refresh_all_tag_presentation_gui' )
        CG.client_controller.sub( self, 'WaterfallThumbnails', 'waterfall_thumbnails' )
        
    
    def _CalculateVisiblePageIndices( self ):
        
        y_start = self._GetYStart()
        
        earliest_y = y_start
        
        last_y = earliest_y + QP.ScrollAreaVisibleRect( self ).size().height()
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        page_height = self._num_rows_per_canvas_page * thumbnail_span_height
        
        first_visible_page_index = earliest_y // page_height
        
        last_visible_page_index = last_y // page_height
        
        page_indices = list( range( first_visible_page_index, last_visible_page_index + 1 ) )
        
        return page_indices
        
    
    def _CreateNewDirtyPage( self ):
        
        my_width = self.size().width()
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        dpr = self.devicePixelRatio()
        
        canvas_width = int( my_width * dpr )
        canvas_height = int( self._num_rows_per_canvas_page * thumbnail_span_height * dpr )
        
        canvas_page = CG.client_controller.bitmap_manager.GetQtImage( canvas_width, canvas_height, 32 )
        
        canvas_page.setDevicePixelRatio( dpr )
        
        self._dirty_canvas_pages.append( canvas_page )
        
    
    def _DeleteAllDirtyPages( self ):
        
        self._dirty_canvas_pages = []
        
    
    def _DirtyAllPages( self ):
        
        clean_indices = list( self._clean_canvas_pages.keys() )
        
        for clean_index in clean_indices:
            
            self._DirtyPage( clean_index )
            
        
    
    def _DirtyPage( self, clean_index ):

        canvas_page = self._clean_canvas_pages[ clean_index ]
        
        del self._clean_canvas_pages[ clean_index ]
        
        thumbnails = [ thumbnail for ( thumbnail_index, thumbnail ) in self._GetThumbnailsFromPageIndex( clean_index ) ]
        
        if len( thumbnails ) > 0:
            
            CG.client_controller.GetCache( 'thumbnail' ).CancelWaterfall( self._page_key, thumbnails )
            
        
        self._dirty_canvas_pages.append( canvas_page )
        
    
    def _DrawCanvasPage( self, page_index, canvas_page ):
        
        painter = QG.QPainter( canvas_page )
        
        new_options = CG.client_controller.new_options
        
        bg_colour = CG.client_controller.new_options.GetColour( CC.COLOUR_THUMBGRID_BACKGROUND )
        
        if HG.thumbnail_debug_mode and page_index % 2 == 0:
            
            bg_colour = ClientGUIFunctions.GetLighterDarkerColour( bg_colour )
            
        
        if new_options.GetNoneableString( 'media_background_bmp_path' ) is not None:
            
            comp_mode = painter.compositionMode()
            
            painter.setCompositionMode( QG.QPainter.CompositionMode_Source )
            
            painter.setBackground( QG.QBrush( QC.Qt.transparent ) )
            
            painter.eraseRect( painter.viewport() )
            
            painter.setCompositionMode( comp_mode )
            
        else: 
            
            painter.setBackground( QG.QBrush( bg_colour ) )
            
            painter.eraseRect( painter.viewport() )
            
        
        #
        
        page_thumbnails = self._GetThumbnailsFromPageIndex( page_index )
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        thumbnails_to_render_later = []
        
        thumbnail_cache = CG.client_controller.GetCache( 'thumbnail' )
        
        thumbnail_margin = CG.client_controller.new_options.GetInteger( 'thumbnail_margin' )
        
        for ( thumbnail_index, thumbnail ) in page_thumbnails:
            
            display_media = thumbnail.GetDisplayMedia()
            
            if display_media is None:
                
                continue
                
            
            hash = display_media.GetHash()
            
            if hash in self._hashes_faded and thumbnail_cache.HasThumbnailCached( thumbnail ):
                
                self._StopFading( hash )
                
                thumbnail_col = thumbnail_index % self._num_columns
                
                thumbnail_row = thumbnail_index // self._num_columns
                
                x = thumbnail_col * thumbnail_span_width + thumbnail_margin
                
                y = ( thumbnail_row - ( page_index * self._num_rows_per_canvas_page ) ) * thumbnail_span_height + thumbnail_margin
                
                painter.drawImage( x, y, thumbnail.GetQtImage( self.devicePixelRatio() ) )
                
            else:
                
                thumbnails_to_render_later.append( thumbnail )
                
            
        
        if len( thumbnails_to_render_later ) > 0:
            
            CG.client_controller.GetCache( 'thumbnail' ).Waterfall( self._page_key, thumbnails_to_render_later )
            
        
    
    def _FadeThumbnails( self, thumbnails ):
        
        if len( thumbnails ) == 0:
            
            return
            
        
        if not CG.client_controller.gui.IsCurrentPage( self._page_key ):
            
            self._DirtyAllPages()
            
            return
            
        
        now_precise = HydrusTime.GetNowPrecise()
        
        for thumbnail in thumbnails:
            
            display_media = thumbnail.GetDisplayMedia()
            
            if display_media is None:
                
                continue
                
            
            try:
                
                thumbnail_index = self._sorted_media.index( thumbnail )
                
            except HydrusExceptions.DataMissing:
                
                # probably means a collect happened during an ongoing waterfall or whatever
                
                continue
                
            
            if self._GetPageIndexFromThumbnailIndex( thumbnail_index ) not in self._clean_canvas_pages:
                
                continue
                
            
            hash = display_media.GetHash()
            
            self._hashes_faded.add( hash )
            
            self._StopFading( hash )
            
            bitmap = thumbnail.GetQtImage( self.devicePixelRatio() )
            
            fade_thumbnails = CG.client_controller.new_options.GetBoolean( 'fade_thumbnails' )
            
            if fade_thumbnails:
                
                thumbnail_draw_object = ThumbnailWaitingToBeDrawnAnimated( hash, thumbnail, thumbnail_index, bitmap )
                
            else:
                
                thumbnail_draw_object = ThumbnailWaitingToBeDrawn( hash, thumbnail, thumbnail_index, bitmap )
                
            
            self._hashes_to_thumbnails_waiting_to_be_drawn[ hash ] = thumbnail_draw_object
            
        
        CG.client_controller.gui.RegisterAnimationUpdateWindow( self )
        
    
    def _GenerateMediaCollection( self, media_results ):
        
        return ThumbnailMediaCollection( self._location_context, media_results )
        
    
    def _GenerateMediaSingleton( self, media_result ):
        
        return ThumbnailMediaSingleton( media_result )
        
    
    def _GetMediaCoordinates( self, media ):
        
        try: index = self._sorted_media.index( media )
        except: return ( -1, -1 )
        
        row = index // self._num_columns
        column = index % self._num_columns
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        thumbnail_margin = CG.client_controller.new_options.GetInteger( 'thumbnail_margin' )
        
        ( x, y ) = ( column * thumbnail_span_width + thumbnail_margin, row * thumbnail_span_height + thumbnail_margin )
        
        return ( x, y )
        
    
    def _GetPageIndexFromThumbnailIndex( self, thumbnail_index ):
        
        thumbnails_per_page = self._num_columns * self._num_rows_per_canvas_page
        
        page_index = thumbnail_index // thumbnails_per_page
        
        return page_index
        
    
    def _GetThumbnailSpanDimensions( self ):
        
        thumbnail_border = CG.client_controller.new_options.GetInteger( 'thumbnail_border' )
        thumbnail_margin = CG.client_controller.new_options.GetInteger( 'thumbnail_margin' )
        
        return ClientData.AddPaddingToDimensions( HC.options[ 'thumbnail_dimensions' ], ( thumbnail_border + thumbnail_margin ) * 2 )
        
    
    def _GetThumbnailUnderMouse( self, mouse_event ):
        
        pos = mouse_event.position().toPoint()
        
        x = pos.x()
        y = pos.y()
        
        ( t_span_x, t_span_y ) = self._GetThumbnailSpanDimensions()
        
        x_mod = x % t_span_x
        y_mod = y % t_span_y
        
        thumbnail_margin = CG.client_controller.new_options.GetInteger( 'thumbnail_margin' )
        
        if x_mod <= thumbnail_margin or y_mod <= thumbnail_margin or x_mod > t_span_x - thumbnail_margin or y_mod > t_span_y - thumbnail_margin:
            
            return None
            
        
        column_index = x // t_span_x
        row_index = y // t_span_y
        
        if column_index >= self._num_columns:
            
            return None
            
        
        thumbnail_index = self._num_columns * row_index + column_index
        
        if thumbnail_index < 0:
            
            return None
            
        
        if thumbnail_index >= len( self._sorted_media ):
            
            return None
            
        
        return self._sorted_media[ thumbnail_index ]
        
    
    def _GetThumbnailsFromPageIndex( self, page_index ):
        
        num_thumbnails_per_page = self._num_columns * self._num_rows_per_canvas_page
        
        start_index = num_thumbnails_per_page * page_index
        
        if start_index <= len( self._sorted_media ):
            
            end_index = min( len( self._sorted_media ), start_index + num_thumbnails_per_page )
            
            thumbnails = [ ( index, self._sorted_media[ index ] ) for index in range( start_index, end_index ) ]
            
        else:
            
            thumbnails = []
            
        
        return thumbnails
        
    
    def _GetYStart( self ):
        
        visible_rect = QP.ScrollAreaVisibleRect( self )
        
        visible_rect_y = visible_rect.y()
        
        visible_rect_height = visible_rect.height()
        
        my_virtual_size = self.widget().size()
        
        my_virtual_height = my_virtual_size.height()
        
        max_y = my_virtual_height - visible_rect_height
        
        y_start = max( 0, visible_rect_y )
        
        y_start = min( y_start, max_y )
        
        return y_start
        
    
    def _MediaIsInCleanPage( self, thumbnail ):
        
        try:
            
            index = self._sorted_media.index( thumbnail )
            
        except HydrusExceptions.DataMissing:
            
            return False
            
        
        if self._GetPageIndexFromThumbnailIndex( index ) in self._clean_canvas_pages:
            
            return True
            
        else:
            
            return False
            
        
    
    def _MediaIsVisible( self, media ):
        
        if media is not None:
            
            ( x, y ) = self._GetMediaCoordinates( media )
            
            visible_rect = QP.ScrollAreaVisibleRect( self )
            
            visible_rect_y = visible_rect.y()
            
            visible_rect_height = visible_rect.height()
            
            ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
            
            bottom_edge_below_top_of_view = visible_rect_y < y + thumbnail_span_height
            top_edge_above_bottom_of_view = y < visible_rect_y + visible_rect_height
            
            is_visible = bottom_edge_below_top_of_view and top_edge_above_bottom_of_view
            
            return is_visible
            
        
        return True
        
    
    def _MoveThumbnailFocus( self, rows, columns, shift ):
        
        if self._last_hit_media is not None:
            
            media_to_use = self._last_hit_media
            
        elif self._next_best_media_if_focuses_removed is not None:
            
            media_to_use = self._next_best_media_if_focuses_removed
            
            if columns == -1: # treat it as if the focused area is between this and the next
                
                columns = 0
                
            
        elif len( self._sorted_media ) > 0:
            
            media_to_use = self._sorted_media[ 0 ]
            
        else:
            
            media_to_use = None
            
        
        if media_to_use is not None:
            
            try:
                
                current_position = self._sorted_media.index( media_to_use )
                
            except HydrusExceptions.DataMissing:
                
                self._SetFocusedMedia( None )
                
                return
                
            
            new_position = current_position + columns + ( self._num_columns * rows )
            
            if new_position < 0:
                
                new_position = 0
                
            elif new_position > len( self._sorted_media ) - 1:
                
                new_position = len( self._sorted_media ) - 1
                
            
            new_media = self._sorted_media[ new_position ]
            
            self._HitMedia( new_media, False, shift )
            
            self._ScrollToMedia( new_media )
            
        
    
    def _NotifyThumbnailsHaveMoved( self ):
        
        self._DirtyAllPages()
        
        self.widget().update()
        
    
    def _RecalculateVirtualSize( self, called_from_resize_event = False ):
        
        my_size = QP.ScrollAreaVisibleRect( self ).size()
        
        my_width = my_size.width()
        my_height = my_size.height()
        
        if my_width > 0 and my_height > 0:
            
            ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
            
            num_media = len( self._sorted_media )
            
            num_rows = max( 1, num_media // self._num_columns )
            
            if num_media % self._num_columns > 0:
                
                num_rows += 1
                
            
            virtual_width = my_width
            
            virtual_height = num_rows * thumbnail_span_height
            
            yUnit = self.verticalScrollBar().singleStep()
            
            excess = virtual_height % yUnit
            
            if excess > 0: # we want virtual height to fit exactly into scroll units, even if that puts some padding below bottom row
                
                top_up = yUnit - excess
                
                virtual_height += top_up
                
            
            virtual_height = max( virtual_height, my_height )
            
            virtual_size = QC.QSize( virtual_width, virtual_height )
            
            if virtual_size != self.widget().size():
                
                self.widget().resize( QC.QSize( virtual_width, virtual_height ) )
                
                if not called_from_resize_event:
                    
                    self._UpdateScrollBars() # would lead to infinite recursion if called from a resize event
                    
                
            
        
    
    def _RedrawMedia( self, thumbnails ):
        
        visible_thumbnails = [ thumbnail for thumbnail in thumbnails if self._MediaIsInCleanPage( thumbnail ) ]
        
        thumbnail_cache = CG.client_controller.GetCache( 'thumbnail' )
        
        thumbnails_to_render_now = []
        thumbnails_to_render_later = []
        
        for thumbnail in visible_thumbnails:
            
            if thumbnail_cache.HasThumbnailCached( thumbnail ):
                
                thumbnails_to_render_now.append( thumbnail )
                
            else:
                
                thumbnails_to_render_later.append( thumbnail )
                
            
        
        if len( thumbnails_to_render_now ) > 0:
            
            self._FadeThumbnails( thumbnails_to_render_now )
            
        
        if len( thumbnails_to_render_later ) > 0:
            
            CG.client_controller.GetCache( 'thumbnail' ).Waterfall( self._page_key, thumbnails_to_render_later )
            
        
    
    def _ReinitialisePageCacheIfNeeded( self ):
        
        old_num_rows = self._num_rows_per_canvas_page
        old_num_columns = self._num_columns
        
        old_width = self._last_size.width()
        old_height = self._last_size.height()
        
        my_size = QP.ScrollAreaVisibleRect( self ).size()
        
        my_width = my_size.width()
        my_height = my_size.height()
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        num_rows = ( my_height // thumbnail_span_height )
        
        self._num_rows_per_actual_page = max( 1, num_rows )
        self._num_rows_per_canvas_page = max( 1, num_rows // 2 )
        
        self._num_columns = max( 1, my_width // thumbnail_span_width )
        
        dimensions_changed = old_width != my_width or old_height != my_height
        thumb_layout_changed = old_num_columns != self._num_columns or old_num_rows != self._num_rows_per_canvas_page
        
        if dimensions_changed or thumb_layout_changed:
            
            width_got_bigger = old_width < my_width
            
            if thumb_layout_changed or width_got_bigger:
                
                self._DirtyAllPages()
                
                self._DeleteAllDirtyPages()
                
            
            self.widget().update()
            
        
    
    def _RemoveMediaDirectly( self, singleton_media, collected_media ):
        
        if self._focused_media is not None:
            
            if self._focused_media in singleton_media or self._focused_media in collected_media:
                
                self._SetFocusedMedia( None )
                
            
        
        MediaPanel._RemoveMediaDirectly( self, singleton_media, collected_media )
        
        self._EndShiftSelect()
        
        self._RecalculateVirtualSize()
        
        self._DirtyAllPages()
        
        self._PublishSelectionChange()
        
        CG.client_controller.pub( 'refresh_page_name', self._page_key )
        
        CG.client_controller.pub( 'notify_new_pages_count' )
        
        self.widget().update()
        
    
    def _ScrollEnd( self, shift = False ):
        
        if len( self._sorted_media ) > 0:
            
            end_media = self._sorted_media[ -1 ]
            
            self._HitMedia( end_media, False, shift )
            
            self._ScrollToMedia( end_media )
            
        
    
    def _ScrollHome( self, shift = False ):
        
        if len( self._sorted_media ) > 0:
            
            home_media = self._sorted_media[ 0 ]
            
            self._HitMedia( home_media, False, shift )
            
            self._ScrollToMedia( home_media )
            
        
    
    def _ScrollToMedia( self, media ):
        
        if media is not None:
            
            ( x, y ) = self._GetMediaCoordinates( media )
            
            visible_rect = QP.ScrollAreaVisibleRect( self )
            
            visible_rect_y = visible_rect.y()
            
            visible_rect_height = visible_rect.height()
            
            ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
            
            new_options = CG.client_controller.new_options
            
            percent_visible = new_options.GetInteger( 'thumbnail_visibility_scroll_percent' ) / 100
            
            if y < visible_rect_y:
                
                self.ensureVisible( 0, y, 0, 0 )
                
            elif y > visible_rect_y + visible_rect_height - ( thumbnail_span_height * percent_visible ):
                
                self.ensureVisible( 0, y + thumbnail_span_height )
                
            
        
    
    def _StopFading( self, hash ):
        
        if hash in self._hashes_to_thumbnails_waiting_to_be_drawn:
            
            del self._hashes_to_thumbnails_waiting_to_be_drawn[ hash ]
            
        
    
    def _UpdateBackgroundColour( self ):
        
        MediaPanel._UpdateBackgroundColour( self )
        
        self._DirtyAllPages()
        
        self._DeleteAllDirtyPages()
        
        self.widget().update()
        
    
    def _UpdateScrollBars( self ):

        # The following call is officially a no-op since this property is already true, but it also triggers an update
        # of the scroll area's scrollbars which we need.
        # We need this since we are intercepting & doing work in resize events which causes
        # event propagation between the scroll area and the scrolled widget to not work properly (since we are suppressing resize events of the scrolled widget - otherwise we would get an infinite loop).
        # Probably the best would be to change how this work and not intercept any resize events.
        # Originally this was wx event handling which got ported to Qt more or less unchanged, hence the hackiness.
        
        self.setWidgetResizable( True )
        
    
    def AddMediaResults( self, page_key, media_results ):
        
        if page_key == self._page_key:
            
            thumbnails = MediaPanel.AddMediaResults( self, page_key, media_results )
            
            if len( thumbnails ) > 0:
                
                self._RecalculateVirtualSize()
                
                CG.client_controller.GetCache( 'thumbnail' ).Waterfall( self._page_key, thumbnails )
                
                if len( self._selected_media ) == 0:
                    
                    self._PublishSelectionIncrement( thumbnails )
                    
                else:
                    
                    self.statusTextChanged.emit( self._GetPrettyStatusForStatusBar() )
                    
                
            
        
    
    def contextMenuEvent( self, event ):
        
        if event.reason() == QG.QContextMenuEvent.Keyboard:
            
            self.ShowMenu()
            
        
    
    def EventMouseFullScreen( self, event ):
        
        t = self._GetThumbnailUnderMouse( event )
        
        if t is not None:
            
            locations_manager = t.GetLocationsManager()
            
            if locations_manager.IsLocal():
                
                self._LaunchMediaViewer( t )
                
            else:
                
                can_download = not locations_manager.GetCurrent().isdisjoint( CG.client_controller.services_manager.GetRemoteFileServiceKeys() )
                
                if can_download:
                    
                    self._DownloadHashes( t.GetHashes() )
                    
                
            
        
    
    def EventResize( self, event ):
        
        self._ReinitialisePageCacheIfNeeded()
        
        self._RecalculateVirtualSize( called_from_resize_event = True )
        
        self._last_size = QP.ScrollAreaVisibleRect( self ).size()
        
    
    def GetTotalFileSize( self ):
        
        return sum( ( m.GetSize() for m in self._sorted_media ) )
        
    
    def MaintainPageCache( self ):
        
        if not CG.client_controller.gui.IsCurrentPage( self._page_key ):
            
            self._DirtyAllPages()
            
        
        self._DeleteAllDirtyPages()
        
    
    def mouseMoveEvent( self, event ):
        
        if event.buttons() & QC.Qt.LeftButton:
            
            we_started_dragging_on_this_panel = self._drag_init_coordinates is not None
            
            if we_started_dragging_on_this_panel:
                
                old_drag_pos = self._drag_init_coordinates
                
                global_mouse_pos = QG.QCursor.pos()
                
                delta_pos = global_mouse_pos - old_drag_pos
                
                total_absolute_pixels_moved = delta_pos.manhattanLength()
                
                we_moved = total_absolute_pixels_moved > 0
                
                if we_moved:
                    
                    self._drag_prefire_event_count += 1
                    
                
                # prefire deal here is mpv lags on initial click, which can cause a drag (and hence an immediate pause) event by accident when mouserelease isn't processed quick
                # so now we'll say we can't start a drag unless we get a smooth ramp to our pixel delta threshold
                clean_drag_started = self._drag_prefire_event_count >= 10
                prob_not_an_accidental_click = HydrusTime.TimeHasPassedMS( self._drag_click_timestamp_ms + 100 )
                
                if clean_drag_started and prob_not_an_accidental_click:
                    
                    media = self._GetSelectedFlatMedia( discriminant = CC.DISCRIMINANT_LOCAL )
                    
                    if len( media ) > 0:
                        
                        alt_down = event.modifiers() & QC.Qt.AltModifier
                        
                        result = ClientGUIDragDrop.DoFileExportDragDrop( self, self._page_key, media, alt_down )
                        
                        if result not in ( QC.Qt.IgnoreAction, ):
                            
                            self.focusMediaPaused.emit()
                            
                        
                    
                
            
        else:
            
            self._drag_init_coordinates = None
            self._drag_prefire_event_count = 0
            self._drag_click_timestamp_ms = 0
            
        
        event.ignore()
        
    
    def mouseReleaseEvent( self, event ):
        
        if event.button() != QC.Qt.RightButton:
            
            QW.QScrollArea.mouseReleaseEvent( self, event )
            
            return
            
        
        self.ShowMenu()
        
    
    def MoveMedia( self, medias: typing.List[ ClientMedia.Media ], insertion_index: int ):
        
        MediaPanel.MoveMedia( self, medias, insertion_index )
        
        self._NotifyThumbnailsHaveMoved()
        
        self._ScrollToMedia( medias[0] )
        
    
    def NewThumbnails( self, hashes ):
        
        affected_thumbnails = self._GetMedia( hashes )
        
        if len( affected_thumbnails ) > 0:
            
            self._RedrawMedia( affected_thumbnails )
            
        
    
    def NotifyNewFileInfo( self, hashes ):
        
        def qt_do_update( hashes_to_media_results ):
            
            affected_media = self._GetMedia( set( hashes_to_media_results.keys() ) )
            
            for media in affected_media:
                
                media.UpdateFileInfo( hashes_to_media_results )
                
            
            self._RedrawMedia( affected_media )
            
        
        def do_it( win, callable, affected_hashes ):
            
            media_results = CG.client_controller.Read( 'media_results', affected_hashes )
            
            hashes_to_media_results = { media_result.GetHash() : media_result for media_result in media_results }
            
            CG.client_controller.CallAfterQtSafe( win, 'new file info notification', qt_do_update, hashes_to_media_results )
            
        
        affected_hashes = self._hashes.intersection( hashes )
        
        CG.client_controller.CallToThread( do_it, self, do_it, affected_hashes )
        
    
    def ProcessApplicationCommand( self, command: CAC.ApplicationCommand ):
        
        command_processed = True
        
        if command.IsSimpleCommand():
            
            action = command.GetSimpleAction()
            
            if action == CAC.SIMPLE_MOVE_THUMBNAIL_FOCUS:
                
                ( move_direction, selection_status ) = command.GetSimpleData()
                
                shift = selection_status == CAC.SELECTION_STATUS_SHIFT
                
                if move_direction in ( CAC.MOVE_HOME, CAC.MOVE_END ):
                    
                    if move_direction == CAC.MOVE_HOME:
                        
                        self._ScrollHome( shift )
                        
                    else: # MOVE_END
                        
                        self._ScrollEnd( shift )
                        
                    
                elif move_direction in ( CAC.MOVE_PAGE_UP, CAC.MOVE_PAGE_DOWN ):
                    
                    if move_direction == CAC.MOVE_PAGE_UP:
                        
                        direction = -1
                        
                    else: # MOVE_PAGE_DOWN
                        
                        direction = 1
                        
                    
                    self._MoveThumbnailFocus( self._num_rows_per_actual_page * direction, 0, shift )
                    
                else:
                    
                    if move_direction == CAC.MOVE_LEFT:
                        
                        rows = 0
                        columns = -1
                        
                    elif move_direction == CAC.MOVE_RIGHT:
                        
                        rows = 0
                        columns = 1
                        
                    elif move_direction == CAC.MOVE_UP:
                        
                        rows = -1
                        columns = 0
                        
                    elif move_direction == CAC.MOVE_DOWN:
                        
                        rows = 1
                        columns = 0
                        
                    else:
                        
                        raise NotImplementedError()
                        
                    
                    self._MoveThumbnailFocus( rows, columns, shift )
                    
                
            elif action == CAC.SIMPLE_SELECT_FILES:
                
                file_filter = command.GetSimpleData()
                
                self._Select( file_filter )
                
            else:
                
                command_processed = False
                
            
        else:
            
            command_processed = False
            
        
        if not command_processed:
            
            return MediaPanel.ProcessApplicationCommand( self, command )
            
        else:
            
            return command_processed
            
        
    
    def RedrawAllThumbnails( self ):
        
        self._DirtyAllPages()
        
        for m in self._collected_media:
            
            m.RecalcInternals()
            
        
        for thumbnail in self._sorted_media:
            
            thumbnail.ClearTagSummaryCaches()
            
        
        self.widget().update()
        
    
    def SetFocusedMedia( self, media ):
        
        MediaPanel.SetFocusedMedia( self, media )
        
        if media is None:
            
            self._SetFocusedMedia( None )
            
        else:
            
            try:
                
                my_media = self._GetMedia( media.GetHashes() )[0]
                
                self._HitMedia( my_media, False, False )
                
                self._ScrollToMedia( self._focused_media )
                
            except:
                
                pass
                
            
        
    
    def showEvent( self, event ):
        
        self._UpdateScrollBars()
        
    
    def ShowMenu( self, do_not_show_just_return = False ):
        
        flat_selected_medias = ClientMedia.FlattenMedia( self._selected_media )
        
        all_locations_managers = [ media.GetLocationsManager() for media in ClientMedia.FlattenMedia( self._sorted_media ) ]
        selected_locations_managers = [ media.GetLocationsManager() for media in flat_selected_medias ]
        
        selection_has_local_file_domain = True in ( locations_manager.IsLocal() and not locations_manager.IsTrashed() for locations_manager in selected_locations_managers )
        selection_has_trash = True in ( locations_manager.IsTrashed() for locations_manager in selected_locations_managers )
        selection_has_inbox = True in ( media.HasInbox() for media in self._selected_media )
        selection_has_archive = True in ( media.HasArchive() and media.GetLocationsManager().IsLocal() for media in self._selected_media )
        selection_has_deletion_record = True in ( CC.COMBINED_LOCAL_FILE_SERVICE_KEY in locations_manager.GetDeleted() for locations_manager in selected_locations_managers )
        
        all_file_domains = HydrusData.MassUnion( locations_manager.GetCurrent() for locations_manager in all_locations_managers )
        all_specific_file_domains = all_file_domains.difference( { CC.COMBINED_FILE_SERVICE_KEY, CC.COMBINED_LOCAL_FILE_SERVICE_KEY } )
        
        some_downloading = True in ( locations_manager.IsDownloading() for locations_manager in selected_locations_managers )
        
        has_local = True in ( locations_manager.IsLocal() for locations_manager in all_locations_managers )
        has_remote = True in ( locations_manager.IsRemote() for locations_manager in all_locations_managers )
        
        num_files = self.GetNumFiles()
        num_selected = self._GetNumSelected()
        num_inbox = self.GetNumInbox()
        num_archive = self.GetNumArchive()
        
        multiple_selected = num_selected > 1
        
        menu = ClientGUIMenus.GenerateMenu( self.window() )
        
        if self._HasFocusSingleton():
            
            focus_singleton = self._GetFocusSingleton()
            
            # variables
            
            collections_selected = True in ( media.IsCollection() for media in self._selected_media )
            
            services_manager = CG.client_controller.services_manager
            
            services = services_manager.GetServices()
            
            service_keys_to_names = { service.GetServiceKey() : service.GetName() for service in services }
            
            file_repositories = [ service for service in services if service.GetServiceType() == HC.FILE_REPOSITORY ]
            
            ipfs_services = [ service for service in services if service.GetServiceType() == HC.IPFS ]
            
            local_ratings_services = [ service for service in services if service.GetServiceType() in HC.RATINGS_SERVICES ]
            
            i_can_post_ratings = len( local_ratings_services ) > 0
            
            local_media_file_service_keys = { service.GetServiceKey() for service in services if service.GetServiceType() == HC.LOCAL_FILE_DOMAIN }
            
            file_repository_service_keys = { repository.GetServiceKey() for repository in file_repositories }
            upload_permission_file_service_keys = { repository.GetServiceKey() for repository in file_repositories if repository.HasPermission( HC.CONTENT_TYPE_FILES, HC.PERMISSION_ACTION_CREATE ) }
            petition_resolve_permission_file_service_keys = { repository.GetServiceKey() for repository in file_repositories if repository.HasPermission( HC.CONTENT_TYPE_FILES, HC.PERMISSION_ACTION_MODERATE ) }
            petition_permission_file_service_keys = { repository.GetServiceKey() for repository in file_repositories if repository.HasPermission( HC.CONTENT_TYPE_FILES, HC.PERMISSION_ACTION_PETITION ) } - petition_resolve_permission_file_service_keys
            user_manage_permission_file_service_keys = { repository.GetServiceKey() for repository in file_repositories if repository.HasPermission( HC.CONTENT_TYPE_ACCOUNTS, HC.PERMISSION_ACTION_MODERATE ) }
            ipfs_service_keys = { service.GetServiceKey() for service in ipfs_services }
            
            if multiple_selected:
                
                download_phrase = 'download all possible selected'
                rescind_download_phrase = 'cancel downloads for all possible selected'
                upload_phrase = 'upload all possible selected to'
                rescind_upload_phrase = 'rescind pending selected uploads to'
                petition_phrase = 'petition all possible selected for removal from'
                rescind_petition_phrase = 'rescind selected petitions for'
                remote_delete_phrase = 'delete all possible selected from'
                modify_account_phrase = 'modify the accounts that uploaded selected to'
                
                pin_phrase = 'pin all to'
                rescind_pin_phrase = 'rescind pin to'
                unpin_phrase = 'unpin all from'
                rescind_unpin_phrase = 'rescind unpin from'
                
                archive_phrase = 'archive selected'
                inbox_phrase = 're-inbox selected'
                local_delete_phrase = 'delete selected'
                delete_physically_phrase = 'delete selected physically now'
                undelete_phrase = 'undelete selected'
                clear_deletion_phrase = 'clear deletion record for selected'
                
            else:
                
                download_phrase = 'download'
                rescind_download_phrase = 'cancel download'
                upload_phrase = 'upload to'
                rescind_upload_phrase = 'rescind pending upload to'
                petition_phrase = 'petition for removal from'
                rescind_petition_phrase = 'rescind petition for'
                remote_delete_phrase = 'delete from'
                modify_account_phrase = 'modify the account that uploaded this to'
                
                pin_phrase = 'pin to'
                rescind_pin_phrase = 'rescind pin to'
                unpin_phrase = 'unpin from'
                rescind_unpin_phrase = 'rescind unpin from'
                
                archive_phrase = 'archive'
                inbox_phrase = 're-inbox'
                local_delete_phrase = 'delete'
                delete_physically_phrase = 'delete physically now'
                undelete_phrase = 'undelete'
                clear_deletion_phrase = 'clear deletion record'
                
            
            # info about the files
            
            remote_service_keys = CG.client_controller.services_manager.GetRemoteFileServiceKeys()
            
            groups_of_current_remote_service_keys = [ locations_manager.GetCurrent().intersection( remote_service_keys ) for locations_manager in selected_locations_managers ]
            groups_of_pending_remote_service_keys = [ locations_manager.GetPending().intersection( remote_service_keys ) for locations_manager in selected_locations_managers ]
            groups_of_petitioned_remote_service_keys = [ locations_manager.GetPetitioned().intersection( remote_service_keys ) for locations_manager in selected_locations_managers ]
            groups_of_deleted_remote_service_keys = [ locations_manager.GetDeleted().intersection( remote_service_keys ) for locations_manager in selected_locations_managers ]
            
            current_remote_service_keys = HydrusData.MassUnion( groups_of_current_remote_service_keys )
            pending_remote_service_keys = HydrusData.MassUnion( groups_of_pending_remote_service_keys )
            petitioned_remote_service_keys = HydrusData.MassUnion( groups_of_petitioned_remote_service_keys )
            deleted_remote_service_keys = HydrusData.MassUnion( groups_of_deleted_remote_service_keys )
            
            common_current_remote_service_keys = HydrusData.IntelligentMassIntersect( groups_of_current_remote_service_keys )
            common_pending_remote_service_keys = HydrusData.IntelligentMassIntersect( groups_of_pending_remote_service_keys )
            common_petitioned_remote_service_keys = HydrusData.IntelligentMassIntersect( groups_of_petitioned_remote_service_keys )
            common_deleted_remote_service_keys = HydrusData.IntelligentMassIntersect( groups_of_deleted_remote_service_keys )
            
            disparate_current_remote_service_keys = current_remote_service_keys - common_current_remote_service_keys
            disparate_pending_remote_service_keys = pending_remote_service_keys - common_pending_remote_service_keys
            disparate_petitioned_remote_service_keys = petitioned_remote_service_keys - common_petitioned_remote_service_keys
            disparate_deleted_remote_service_keys = deleted_remote_service_keys - common_deleted_remote_service_keys
            
            pending_file_service_keys = pending_remote_service_keys.intersection( file_repository_service_keys )
            petitioned_file_service_keys = petitioned_remote_service_keys.intersection( file_repository_service_keys )
            
            common_current_file_service_keys = common_current_remote_service_keys.intersection( file_repository_service_keys )
            common_pending_file_service_keys = common_pending_remote_service_keys.intersection( file_repository_service_keys )
            common_petitioned_file_service_keys = common_petitioned_remote_service_keys.intersection( file_repository_service_keys )
            common_deleted_file_service_keys = common_deleted_remote_service_keys.intersection( file_repository_service_keys )
            
            disparate_current_file_service_keys = disparate_current_remote_service_keys.intersection( file_repository_service_keys )
            disparate_pending_file_service_keys = disparate_pending_remote_service_keys.intersection( file_repository_service_keys )
            disparate_petitioned_file_service_keys = disparate_petitioned_remote_service_keys.intersection( file_repository_service_keys )
            disparate_deleted_file_service_keys = disparate_deleted_remote_service_keys.intersection( file_repository_service_keys )
            
            pending_ipfs_service_keys = pending_remote_service_keys.intersection( ipfs_service_keys )
            petitioned_ipfs_service_keys = petitioned_remote_service_keys.intersection( ipfs_service_keys )
            
            common_current_ipfs_service_keys = common_current_remote_service_keys.intersection( ipfs_service_keys )
            common_pending_ipfs_service_keys = common_pending_file_service_keys.intersection( ipfs_service_keys )
            common_petitioned_ipfs_service_keys = common_petitioned_remote_service_keys.intersection( ipfs_service_keys )
            
            disparate_current_ipfs_service_keys = disparate_current_remote_service_keys.intersection( ipfs_service_keys )
            disparate_pending_ipfs_service_keys = disparate_pending_remote_service_keys.intersection( ipfs_service_keys )
            disparate_petitioned_ipfs_service_keys = disparate_petitioned_remote_service_keys.intersection( ipfs_service_keys )
            
            # valid commands for the files
            
            current_file_service_keys = set()
            
            uploadable_file_service_keys = set()
            
            downloadable_file_service_keys = set()
            
            petitionable_file_service_keys = set()
            
            deletable_file_service_keys = set()
            
            modifyable_file_service_keys = set()
            
            pinnable_ipfs_service_keys = set()
            
            unpinnable_ipfs_service_keys = set()
            
            remote_file_service_keys = ipfs_service_keys.union( file_repository_service_keys )
            
            for locations_manager in selected_locations_managers:
                
                current = locations_manager.GetCurrent()
                deleted = locations_manager.GetDeleted()
                pending = locations_manager.GetPending()
                petitioned = locations_manager.GetPetitioned()
                
                # ALL
                
                current_file_service_keys.update( current )
                
                # FILE REPOS
                
                # we can upload (set pending) to a repo_id when we have permission, a file is local, not current, not pending, and either ( not deleted or we_can_overrule )
                
                if locations_manager.IsLocal():
                    
                    cannot_upload_to = current.union( pending ).union( deleted.difference( petition_resolve_permission_file_service_keys ) )
                    
                    can_upload_to = upload_permission_file_service_keys.difference( cannot_upload_to )
                    
                    uploadable_file_service_keys.update( can_upload_to )
                    
                
                # we can download (set pending to local) when we have permission, a file is not local and not already downloading and current
                
                if not locations_manager.IsLocal() and not locations_manager.IsDownloading():
                    
                    downloadable_file_service_keys.update( remote_file_service_keys.intersection( current ) )
                    
                
                # we can petition when we have permission and a file is current and it is not already petitioned
                
                petitionable_file_service_keys.update( ( petition_permission_file_service_keys & current ) - petitioned )
                
                # we can delete remote when we have permission and a file is current and it is not already petitioned
                
                deletable_file_service_keys.update( ( petition_resolve_permission_file_service_keys & current ) - petitioned )
                
                # we can modify users when we have permission and the file is current or deleted
                
                modifyable_file_service_keys.update( user_manage_permission_file_service_keys & ( current | deleted ) )
                
                # IPFS
                
                # we can pin if a file is local, not current, not pending
                
                if locations_manager.IsLocal():
                    
                    pinnable_ipfs_service_keys.update( ipfs_service_keys - current - pending )
                    
                
                # we can unpin a file if it is current and not petitioned
                
                unpinnable_ipfs_service_keys.update( ( ipfs_service_keys & current ) - petitioned )
                
            
            # do the actual menu
            
            selection_info_menu = ClientGUIMenus.GenerateMenu( menu )
            
            selected_files_string = ClientMedia.GetMediasFiletypeSummaryString( self._selected_media )
            
            selection_info_menu_label = f'{selected_files_string}, {self._GetPrettyTotalSize( only_selected = True )}'
            
            if multiple_selected:
                
                pretty_total_duration = self._GetPrettyTotalDuration( only_selected = True )
                
                if pretty_total_duration != '':
                    
                    selection_info_menu_label += ', {}'.format( pretty_total_duration )
                    
                
            else:
                
                # TODO: move away from this hell function GetPrettyInfoLines and set the timestamp tooltips to the be the full ISO time
                
                pretty_info_lines = list( focus_singleton.GetPrettyInfoLines() )
                
                ClientGUIMediaMenus.AddPrettyInfoLines( selection_info_menu, pretty_info_lines )
                
            
            ClientGUIMenus.AppendSeparator( selection_info_menu )
            
            ClientGUIMediaMenus.AddFileViewingStatsMenu( selection_info_menu, self._selected_media )
            
            if len( disparate_current_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_current_file_service_keys, 'some uploaded to' )
                
            
            if multiple_selected and len( common_current_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_current_file_service_keys, 'selected uploaded to' )
                
            
            if len( disparate_pending_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_pending_file_service_keys, 'some pending to' )
                
            
            if len( common_pending_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_pending_file_service_keys, 'pending to' )
                
            
            if len( disparate_petitioned_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_petitioned_file_service_keys, 'some petitioned for removal from' )
                
            
            if len( common_petitioned_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_petitioned_file_service_keys, 'petitioned for removal from' )
                
            
            if len( disparate_deleted_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_deleted_file_service_keys, 'some deleted from' )
                
            
            if len( common_deleted_file_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_deleted_file_service_keys, 'deleted from' )
                
            
            if len( disparate_current_ipfs_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_current_ipfs_service_keys, 'some pinned to' )
                
            
            if multiple_selected and len( common_current_ipfs_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_current_ipfs_service_keys, 'selected pinned to' )
                
            
            if len( disparate_pending_ipfs_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_pending_ipfs_service_keys, 'some to be pinned to' )
                
            
            if len( common_pending_ipfs_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_pending_ipfs_service_keys, 'to be pinned to' )
                
            
            if len( disparate_petitioned_ipfs_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, disparate_petitioned_ipfs_service_keys, 'some to be unpinned from' )
                
            
            if len( common_petitioned_ipfs_service_keys ) > 0:
                
                ClientGUIMediaMenus.AddServiceKeyLabelsToMenu( selection_info_menu, common_petitioned_ipfs_service_keys, unpin_phrase )
                
            
            if len( selection_info_menu.actions() ) == 0:
                
                selection_info_menu.deleteLater()
                
                ClientGUIMenus.AppendMenuLabel( menu, selection_info_menu_label )
                
            else:
                
                ClientGUIMenus.AppendMenu( menu, selection_info_menu, selection_info_menu_label )
                
            
        
        ClientGUIMenus.AppendSeparator( menu )
        
        ClientGUIMenus.AppendMenuItem( menu, 'refresh', 'Refresh the current search.', self.refreshQuery.emit )
        
        if len( self._sorted_media ) > 0:
            
            ClientGUIMenus.AppendSeparator( menu )
            
            filter_counts = {}
            
            filter_counts[ ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_ALL ) ] = num_files
            filter_counts[ ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_INBOX ) ] = num_inbox
            filter_counts[ ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_ARCHIVE ) ] = num_archive
            filter_counts[ ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_SELECTED ) ] = num_selected
            
            has_local_and_remote = has_local and has_remote
            
            AddSelectMenu( self, menu, filter_counts, all_specific_file_domains, has_local_and_remote )
            AddRemoveMenu( self, menu, filter_counts, all_specific_file_domains, has_local_and_remote )
            
            if len( self._selected_media ) > 0:
                
                ordered_selected_media = self._GetSelectedMediaOrdered()
                
                try:
                    
                    earliest_index = self._sorted_media.index( ordered_selected_media[0] )
                    
                    num_selected = len( self._selected_media )
                    
                    selection_is_contiguous = num_selected > 0 and self._sorted_media.index( ordered_selected_media[-1] ) - earliest_index == num_selected - 1
                    
                    AddMoveMenu( self, menu, self._selected_media, self._sorted_media, self._focused_media, selection_is_contiguous, earliest_index )
                    
                except HydrusExceptions.DataMissing:
                    
                    pass
                    
                
            
            ClientGUIMenus.AppendSeparator( menu )
            
            if has_local:
                
                ClientGUIMenus.AppendMenuItem( menu, 'archive/delete filter', 'Launch a special media viewer that will quickly archive (left-click) and delete (right-click) the selected media.', self._ArchiveDeleteFilter )
                
            
        
        if self._HasFocusSingleton():
            
            focus_singleton = self._GetFocusSingleton()
            
            if selection_has_inbox:
                
                ClientGUIMenus.AppendMenuItem( menu, archive_phrase, 'Archive the selected files.', self._Archive )
                
            
            if selection_has_archive:
                
                ClientGUIMenus.AppendMenuItem( menu, inbox_phrase, 'Put the selected files back in the inbox.', self._Inbox )
                
            
            ClientGUIMenus.AppendSeparator( menu )
            
            user_command_deletable_file_service_keys = local_media_file_service_keys.union( [ CC.LOCAL_UPDATE_SERVICE_KEY ] )
            
            local_file_service_keys_we_are_in = sorted( current_file_service_keys.intersection( user_command_deletable_file_service_keys ), key = CG.client_controller.services_manager.GetName )
            
            if len( local_file_service_keys_we_are_in ) > 0:
                
                delete_menu = ClientGUIMenus.GenerateMenu( menu )
                
                for file_service_key in local_file_service_keys_we_are_in:
                    
                    service_name = CG.client_controller.services_manager.GetName( file_service_key )
                    
                    ClientGUIMenus.AppendMenuItem( delete_menu, f'from {service_name}', f'Delete the selected files from {service_name}.', self._Delete, file_service_key )
                    
                
                ClientGUIMenus.AppendMenu( menu, delete_menu, local_delete_phrase )
                
            
            if selection_has_trash:
                
                if selection_has_local_file_domain:
                    
                    ClientGUIMenus.AppendMenuItem( menu, 'delete trash physically now', 'Completely delete the selected trashed files, forcing an immediate physical delete from your hard drive.', self._Delete, CC.COMBINED_LOCAL_FILE_SERVICE_KEY, only_those_in_file_service_key = CC.TRASH_SERVICE_KEY )
                    
                
                ClientGUIMenus.AppendMenuItem( menu, delete_physically_phrase, 'Completely delete the selected files, forcing an immediate physical delete from your hard drive.', self._Delete, CC.COMBINED_LOCAL_FILE_SERVICE_KEY )
                ClientGUIMenus.AppendMenuItem( menu, undelete_phrase, 'Restore the selected files back to \'my files\'.', self._Undelete )
                
            
            if selection_has_deletion_record:
                
                ClientGUIMenus.AppendMenuItem( menu, clear_deletion_phrase, 'Clear the deletion record for these files, allowing them to reimport even if previously deleted files are set to be discarded.', self._ClearDeleteRecord )
                
            
            #
            
            ClientGUIMenus.AppendSeparator( menu )
            
            manage_menu = ClientGUIMenus.GenerateMenu( menu )
            
            ClientGUIMenus.AppendMenuItem( manage_menu, 'tags', 'Manage tags for the selected files.', self._ManageTags )
            
            if i_can_post_ratings:
                
                ClientGUIMenus.AppendMenuItem( manage_menu, 'ratings', 'Manage ratings for the selected files.', self._ManageRatings )
                
            
            ClientGUIMenus.AppendMenuItem( manage_menu, 'urls', 'Manage urls for the selected files.', self._ManageURLs )
            
            num_notes = focus_singleton.GetNotesManager().GetNumNotes()
            
            notes_str = 'notes'
            
            if num_notes > 0:
                
                notes_str = '{} ({})'.format( notes_str, HydrusData.ToHumanInt( num_notes ) )
                
            
            ClientGUIMenus.AppendMenuItem( manage_menu, notes_str, 'Manage notes for the focused file.', self._ManageNotes )
            
            ClientGUIMenus.AppendMenuItem( manage_menu, 'times', 'Edit the timestamps for your files.', self._ManageTimestamps )
            ClientGUIMenus.AppendMenuItem( manage_menu, 'force filetype', 'Force your files to appear as a different filetype.', ClientGUIMediaModalActions.SetFilesForcedFiletypes, self, self._selected_media )
            
            ClientGUIMediaMenus.AddDuplicatesMenu( self, manage_menu, self._location_context, focus_singleton, num_selected, collections_selected )
            
            regen_menu = ClientGUIMenus.GenerateMenu( manage_menu )
            
            for job_type in ClientFiles.ALL_REGEN_JOBS_IN_HUMAN_ORDER:
                
                ClientGUIMenus.AppendMenuItem( regen_menu, ClientFiles.regen_file_enum_to_str_lookup[ job_type ], ClientFiles.regen_file_enum_to_description_lookup[ job_type ], self._RegenerateFileData, job_type )
                
            
            ClientGUIMenus.AppendMenu( manage_menu, regen_menu, 'maintenance' )
            
            ClientGUIMediaMenus.AddManageFileViewingStatsMenu( self, manage_menu, flat_selected_medias )
            
            ClientGUIMenus.AppendMenu( menu, manage_menu, 'manage' )
            
            ( local_duplicable_to_file_service_keys, local_moveable_from_and_to_file_service_keys ) = ClientGUIMediaSimpleActions.GetLocalFileActionServiceKeys( flat_selected_medias )
            
            len_interesting_local_service_keys = 0
            
            len_interesting_local_service_keys += len( local_duplicable_to_file_service_keys )
            len_interesting_local_service_keys += len( local_moveable_from_and_to_file_service_keys )
            
            #
            
            len_interesting_remote_service_keys = 0
            
            len_interesting_remote_service_keys += len( downloadable_file_service_keys )
            len_interesting_remote_service_keys += len( uploadable_file_service_keys )
            len_interesting_remote_service_keys += len( pending_file_service_keys )
            len_interesting_remote_service_keys += len( petitionable_file_service_keys )
            len_interesting_remote_service_keys += len( petitioned_file_service_keys )
            len_interesting_remote_service_keys += len( deletable_file_service_keys )
            len_interesting_remote_service_keys += len( modifyable_file_service_keys )
            len_interesting_remote_service_keys += len( pinnable_ipfs_service_keys )
            len_interesting_remote_service_keys += len( pending_ipfs_service_keys )
            len_interesting_remote_service_keys += len( unpinnable_ipfs_service_keys )
            len_interesting_remote_service_keys += len( petitioned_ipfs_service_keys )
            
            if multiple_selected:
                
                len_interesting_remote_service_keys += len( ipfs_service_keys )
                
            
            if len_interesting_local_service_keys > 0 or len_interesting_remote_service_keys > 0:
                
                files_menu = ClientGUIMenus.GenerateMenu( menu )
                
                ClientGUIMenus.AppendMenu( menu, files_menu, 'files' )
                
                if len_interesting_local_service_keys > 0:
                    
                    ClientGUIMediaMenus.AddLocalFilesMoveAddToMenu( self, files_menu, local_duplicable_to_file_service_keys, local_moveable_from_and_to_file_service_keys, multiple_selected, self.ProcessApplicationCommand )
                    
                
                if len_interesting_remote_service_keys > 0:
                    
                    ClientGUIMenus.AppendSeparator( files_menu )
                    
                    if len( downloadable_file_service_keys ) > 0:
                        
                        ClientGUIMenus.AppendMenuItem( files_menu, download_phrase, 'Download all possible selected files.', self._DownloadSelected )
                        
                    
                    if some_downloading:
                        
                        ClientGUIMenus.AppendMenuItem( files_menu, rescind_download_phrase, 'Stop downloading any of the selected files.', self._RescindDownloadSelected )
                        
                    
                    if len( uploadable_file_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, uploadable_file_service_keys, upload_phrase, 'Upload all selected files to the file repository.', self._UploadFiles )
                        
                    
                    if len( pending_file_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, pending_file_service_keys, rescind_upload_phrase, 'Rescind the pending upload to the file repository.', self._RescindUploadFiles )
                        
                    
                    if len( petitionable_file_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, petitionable_file_service_keys, petition_phrase, 'Petition these files for deletion from the file repository.', self._PetitionFiles )
                        
                    
                    if len( petitioned_file_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, petitioned_file_service_keys, rescind_petition_phrase, 'Rescind the petition to delete these files from the file repository.', self._RescindPetitionFiles )
                        
                    
                    if len( deletable_file_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, deletable_file_service_keys, remote_delete_phrase, 'Delete these files from the file repository.', self._Delete )
                        
                    
                    if len( modifyable_file_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, modifyable_file_service_keys, modify_account_phrase, 'Modify the account(s) that uploaded these files to the file repository.', self._ModifyUploaders )
                        
                    
                    if len( pinnable_ipfs_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, pinnable_ipfs_service_keys, pin_phrase, 'Pin these files to the ipfs service.', self._UploadFiles )
                        
                    
                    if len( pending_ipfs_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, pending_ipfs_service_keys, rescind_pin_phrase, 'Rescind the pending pin to the ipfs service.', self._RescindUploadFiles )
                        
                    
                    if len( unpinnable_ipfs_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, unpinnable_ipfs_service_keys, unpin_phrase, 'Unpin these files from the ipfs service.', self._PetitionFiles )
                        
                    
                    if len( petitioned_ipfs_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, petitioned_ipfs_service_keys, rescind_unpin_phrase, 'Rescind the pending unpin from the ipfs service.', self._RescindPetitionFiles )
                        
                    
                    if multiple_selected and len( ipfs_service_keys ) > 0:
                        
                        ClientGUIMediaMenus.AddServiceKeysToMenu( files_menu, ipfs_service_keys, 'pin new directory to', 'Pin these files as a directory to the ipfs service.', self._UploadDirectory )
                        
                    
                
            
            #
            
            ClientGUIMediaMenus.AddKnownURLsViewCopyMenu( self, menu, self._focused_media, selected_media = self._selected_media )
            
            ClientGUIMediaMenus.AddOpenMenu( self, menu, self._focused_media, self._selected_media )
            
            ClientGUIMediaMenus.AddShareMenu( self, menu, self._focused_media, self._selected_media )
            
        
        if not do_not_show_just_return:
            
            CGC.core().PopupMenu( self, menu )
            
        
        else:
            
            return menu
            
        
    
    def Sort( self, media_sort = None ):
        
        MediaPanel.Sort( self, media_sort )
        
        self._NotifyThumbnailsHaveMoved()
        
    
    def ThumbnailsReset( self ):
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        thumbnail_scroll_rate = float( CG.client_controller.new_options.GetString( 'thumbnail_scroll_rate' ) )
        
        self.verticalScrollBar().setSingleStep( int( round( thumbnail_span_height * thumbnail_scroll_rate ) ) )
        
        self._hashes_to_thumbnails_waiting_to_be_drawn = {}
        self._hashes_faded = set()
        
        self._ReinitialisePageCacheIfNeeded()
        
        self._RecalculateVirtualSize()
        
        self.RedrawAllThumbnails()
        
    
    def TIMERAnimationUpdate( self ):
        
        loop_should_break_time = HydrusTime.GetNowPrecise() + ( FRAME_DURATION_60FPS / 2 )
        
        ( thumbnail_span_width, thumbnail_span_height ) = self._GetThumbnailSpanDimensions()
        
        thumbnail_margin = CG.client_controller.new_options.GetInteger( 'thumbnail_margin' )
        
        hashes = list( self._hashes_to_thumbnails_waiting_to_be_drawn.keys() )
        
        page_indices_to_painters = {}
        
        page_height = self._num_rows_per_canvas_page * thumbnail_span_height
        
        for hash in HydrusData.IterateListRandomlyAndFast( hashes ):
            
            thumbnail_draw_object = self._hashes_to_thumbnails_waiting_to_be_drawn[ hash ]
            
            delete_entry = False
            
            if thumbnail_draw_object.DrawDue():
                
                thumbnail_index = thumbnail_draw_object.thumbnail_index
                
                try:
                    
                    expected_thumbnail = self._sorted_media[ thumbnail_index ]
                    
                except:
                    
                    expected_thumbnail = None
                    
                
                page_index = self._GetPageIndexFromThumbnailIndex( thumbnail_index )
                
                if expected_thumbnail != thumbnail_draw_object.thumbnail:
                    
                    delete_entry = True
                    
                elif page_index not in self._clean_canvas_pages:
                    
                    delete_entry = True
                    
                else:
                    
                    thumbnail_col = thumbnail_index % self._num_columns
                    
                    thumbnail_row = thumbnail_index // self._num_columns
                    
                    x = thumbnail_col * thumbnail_span_width + thumbnail_margin
                    
                    y = ( thumbnail_row - ( page_index * self._num_rows_per_canvas_page ) ) * thumbnail_span_height + thumbnail_margin
                    
                    if page_index not in page_indices_to_painters:
                        
                        canvas_page = self._clean_canvas_pages[ page_index ]
                        
                        painter = QG.QPainter( canvas_page )
                        
                        page_indices_to_painters[ page_index ] = painter
                        
                    
                    painter = page_indices_to_painters[ page_index ]
                    
                    thumbnail_draw_object.DrawToPainter( x, y, painter )
                    
                    #
                    
                    page_virtual_y = page_height * page_index
                    
                    self.widget().update( QC.QRect( x, page_virtual_y + y, thumbnail_span_width - thumbnail_margin, thumbnail_span_height - thumbnail_margin ) )
                    
                
            
            if thumbnail_draw_object.DrawComplete() or delete_entry:
                
                del self._hashes_to_thumbnails_waiting_to_be_drawn[ hash ]
                
            
            if HydrusTime.TimeHasPassedPrecise( loop_should_break_time ):
                
                break
                
            
        
        if len( self._hashes_to_thumbnails_waiting_to_be_drawn ) == 0:
            
            CG.client_controller.gui.UnregisterAnimationUpdateWindow( self )
            
        
        
    
    def WaterfallThumbnails( self, page_key, thumbnails ):
        
        if self._page_key == page_key:
            
            self._FadeThumbnails( thumbnails )
            
        
    
    class _InnerWidget( QW.QWidget ):
        
        def __init__( self, parent ):
            
            QW.QWidget.__init__( self, parent )
            
            self._parent = parent
            
        
        def mousePressEvent( self, event ):
            
            self._parent._drag_init_coordinates = QG.QCursor.pos()
            self._parent._drag_click_timestamp_ms = HydrusTime.GetNowMS()
            
            thumb = self._parent._GetThumbnailUnderMouse( event )
            
            right_on_whitespace = event.button() == QC.Qt.RightButton and thumb is None
            
            if not right_on_whitespace:
                
                self._parent._HitMedia( thumb, event.modifiers() & QC.Qt.ControlModifier, event.modifiers() & QC.Qt.ShiftModifier )
                
            
            # this specifically does not scroll to media, as for clicking (esp. double-clicking attempts), the scroll can be jarring
            
        
        def paintEvent( self, event ):
            
            if self._parent.devicePixelRatio() != self._parent._last_device_pixel_ratio:
                
                self._parent._last_device_pixel_ratio = self._parent.devicePixelRatio()
                
                self._parent._DirtyAllPages()
                self._parent._DeleteAllDirtyPages()
                
            
            painter = QG.QPainter( self )
            
            ( thumbnail_span_width, thumbnail_span_height ) = self._parent._GetThumbnailSpanDimensions()
            
            page_height = self._parent._num_rows_per_canvas_page * thumbnail_span_height
            
            page_indices_to_display = self._parent._CalculateVisiblePageIndices()
            
            earliest_page_index_to_display = min( page_indices_to_display )
            last_page_index_to_display = max( page_indices_to_display )
            
            page_indices_to_draw = list( page_indices_to_display )
            
            if earliest_page_index_to_display > 0:
                
                page_indices_to_draw.append( earliest_page_index_to_display - 1 )
                
            
            page_indices_to_draw.append( last_page_index_to_display + 1 )
            
            page_indices_to_draw.sort()
            
            potential_clean_indices_to_steal = [ page_index for page_index in self._parent._clean_canvas_pages.keys() if page_index not in page_indices_to_draw ]
            
            random.shuffle( potential_clean_indices_to_steal )
            
            y_start = self._parent._GetYStart()
            
            bg_colour = CG.client_controller.new_options.GetColour( CC.COLOUR_THUMBGRID_BACKGROUND )
            
            painter.setBackground( QG.QBrush( bg_colour ) )
            
            painter.eraseRect( painter.viewport() )
            
            background_pixmap = CG.client_controller.bitmap_manager.GetMediaBackgroundPixmap()
            
            if background_pixmap is not None:
                
                my_size = QP.ScrollAreaVisibleRect( self._parent ).size()
                
                pixmap_size = background_pixmap.size()
                
                painter.drawPixmap( my_size.width() - pixmap_size.width(), my_size.height() - pixmap_size.height(), background_pixmap )
                
            
            for page_index in page_indices_to_draw:
                
                if page_index not in self._parent._clean_canvas_pages:
                    
                    if len( self._parent._dirty_canvas_pages ) == 0:
                        
                        if len( potential_clean_indices_to_steal ) > 0:
                            
                            index_to_steal = potential_clean_indices_to_steal.pop()
                            
                            self._parent._DirtyPage( index_to_steal )
                            
                        else:
                            
                            self._parent._CreateNewDirtyPage()
                            
                        
                    
                    canvas_page = self._parent._dirty_canvas_pages.pop()
                    
                    self._parent._DrawCanvasPage( page_index, canvas_page )
                    
                    self._parent._clean_canvas_pages[ page_index ] = canvas_page
                    
                
                if page_index in page_indices_to_display:
                    
                    canvas_page = self._parent._clean_canvas_pages[ page_index ]
                    
                    page_virtual_y = page_height * page_index
                    
                    painter.drawImage( 0, page_virtual_y, canvas_page )
                    
                
            
        
    

def AddMoveMenu( win: MediaPanel, menu: QW.QMenu, selected_media: typing.Set[ ClientMedia.Media ], sorted_media: ClientMedia.SortedList, focused_media: typing.Optional[ ClientMedia.Media ], selection_is_contiguous: bool, earliest_index: int ):
    
    if len( selected_media ) == 0 or len( selected_media ) == len( sorted_media ):
        
        return
        
    
    move_menu = ClientGUIMenus.GenerateMenu( menu )
    
    if earliest_index > 0:
        
        ClientGUIMenus.AppendMenuItem(
            move_menu,
            'to start',
            'Move the selected thumbnails to the start of the media list.',
            win.ProcessApplicationCommand,
            CAC.ApplicationCommand.STATICCreateSimpleCommand( CAC.SIMPLE_REARRANGE_THUMBNAILS, ( CAC.REARRANGE_THUMBNAILS_TYPE_COMMAND, CAC.MOVE_HOME ) )
        )
        
        ClientGUIMenus.AppendMenuItem(
            move_menu,
            'back one',
            'Move the selected thumbnails back one position.',
            win.ProcessApplicationCommand,
            CAC.ApplicationCommand.STATICCreateSimpleCommand( CAC.SIMPLE_REARRANGE_THUMBNAILS, ( CAC.REARRANGE_THUMBNAILS_TYPE_COMMAND, CAC.MOVE_LEFT ) )
        )
        
    
    if focused_media is not None:
        
        try:
            
            focused_index = sorted_media.index( focused_media )
            
            if focused_index != earliest_index or not selection_is_contiguous:
                
                ClientGUIMenus.AppendMenuItem(
                    move_menu,
                    'to here',
                    'Move the selected thumbnails to the focused position (most likely the one you clicked on).',
                    win.ProcessApplicationCommand,
                    CAC.ApplicationCommand.STATICCreateSimpleCommand( CAC.SIMPLE_REARRANGE_THUMBNAILS, ( CAC.REARRANGE_THUMBNAILS_TYPE_COMMAND, CAC.MOVE_TO_FOCUS ) )
                )
                
            
        except HydrusExceptions.DataMissing:
            
            pass
            
        
    
    if earliest_index + len( selected_media ) < len( sorted_media ):
        
        ClientGUIMenus.AppendMenuItem(
            move_menu,
            'forward one',
            'Move the selected thumbnails forward one position.',
            win.ProcessApplicationCommand,
            CAC.ApplicationCommand.STATICCreateSimpleCommand( CAC.SIMPLE_REARRANGE_THUMBNAILS, ( CAC.REARRANGE_THUMBNAILS_TYPE_COMMAND, CAC.MOVE_RIGHT ) )
        )
        
        ClientGUIMenus.AppendMenuItem(
            move_menu,
            'to end',
            'Move the selected thumbnails to the end of the media list.',
            win.ProcessApplicationCommand,
            CAC.ApplicationCommand.STATICCreateSimpleCommand( CAC.SIMPLE_REARRANGE_THUMBNAILS, ( CAC.REARRANGE_THUMBNAILS_TYPE_COMMAND, CAC.MOVE_END ) )
        )
        
    
    ClientGUIMenus.AppendMenu( menu, move_menu, 'move' )
    

def AddRemoveMenu( win: MediaPanel, menu: QW.QMenu, filter_counts, all_specific_file_domains, has_local_and_remote ):
    
    file_filter_all = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_ALL )
    
    if file_filter_all.GetCount( win, filter_counts ) > 0:
        
        remove_menu = ClientGUIMenus.GenerateMenu( menu )
        
        #
        
        file_filter_selected = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_SELECTED )
        
        file_filter_inbox = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_INBOX )
        
        file_filter_archive = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_ARCHIVE )
        
        file_filter_not_selected = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_NOT_SELECTED )
        
        #
        
        selected_count = file_filter_selected.GetCount( win, filter_counts )
        
        if 0 < selected_count < file_filter_all.GetCount( win, filter_counts ):
            
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_selected.ToStringWithCount( win, filter_counts ), 'Remove all the selected files from the current view.', win._Remove, file_filter_selected )
            
        
        if file_filter_all.GetCount( win, filter_counts ) > 0:
            
            ClientGUIMenus.AppendSeparator( remove_menu )
            
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_all.ToStringWithCount( win, filter_counts ), 'Remove all the files from the current view.', win._Remove, file_filter_all )
            
        
        if file_filter_inbox.GetCount( win, filter_counts ) > 0 and file_filter_archive.GetCount( win, filter_counts ) > 0:
            
            ClientGUIMenus.AppendSeparator( remove_menu )
            
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_inbox.ToStringWithCount( win, filter_counts ), 'Remove all the inbox files from the current view.', win._Remove, file_filter_inbox )
            
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_archive.ToStringWithCount( win, filter_counts ), 'Remove all the archived files from the current view.', win._Remove, file_filter_archive )
            
        
        if len( all_specific_file_domains ) > 1:
            
            ClientGUIMenus.AppendSeparator( remove_menu )
            
            all_specific_file_domains = ClientLocation.SortFileServiceKeysNicely( all_specific_file_domains )
            
            all_specific_file_domains = ClientLocation.FilterOutRedundantMetaServices( all_specific_file_domains )
            
            for file_service_key in all_specific_file_domains:
                
                file_filter = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_FILE_SERVICE, file_service_key )
                
                ClientGUIMenus.AppendMenuItem( remove_menu, file_filter.ToStringWithCount( win, filter_counts ), 'Remove all the files that are in this file domain.', win._Remove, file_filter )
                
            
        
        if has_local_and_remote:
            
            file_filter_local = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_LOCAL )
            file_filter_remote = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_REMOTE )
            
            ClientGUIMenus.AppendSeparator( remove_menu )
            
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_local.ToStringWithCount( win, filter_counts ), 'Remove all the files that are in this client.', win._Remove, file_filter_local )
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_remote.ToStringWithCount( win, filter_counts ), 'Remove all the files that are not in this client.', win._Remove, file_filter_remote )
            
        
        not_selected_count = file_filter_not_selected.GetCount( win, filter_counts )
        
        if not_selected_count > 0 and selected_count > 0:
            
            ClientGUIMenus.AppendSeparator( remove_menu )
            
            ClientGUIMenus.AppendMenuItem( remove_menu, file_filter_not_selected.ToStringWithCount( win, filter_counts ), 'Remove all the not selected files from the current view.', win._Remove, file_filter_not_selected )
            
        
        ClientGUIMenus.AppendMenu( menu, remove_menu, 'remove' )
        
    

def AddSelectMenu( win: MediaPanel, menu, filter_counts, all_specific_file_domains, has_local_and_remote ):
    
    file_filter_all = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_ALL )
    
    if file_filter_all.GetCount( win, filter_counts ) > 0:
        
        select_menu = ClientGUIMenus.GenerateMenu( menu )
        
        #
        
        file_filter_inbox = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_INBOX )
        
        file_filter_archive = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_ARCHIVE )
        
        file_filter_not_selected = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_NOT_SELECTED )
        
        file_filter_none = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_NONE )
        
        #
        
        if file_filter_all.GetCount( win, filter_counts ) > 0:
            
            ClientGUIMenus.AppendSeparator( select_menu )
            
            ClientGUIMenus.AppendMenuItem( select_menu, file_filter_all.ToStringWithCount( win, filter_counts ), 'Select all the files in the current view.', win._Select, file_filter_all )
            
        
        if file_filter_inbox.GetCount( win, filter_counts ) > 0 and file_filter_archive.GetCount( win, filter_counts ) > 0:
            
            ClientGUIMenus.AppendSeparator( select_menu )
            
            ClientGUIMenus.AppendMenuItem( select_menu, file_filter_inbox.ToStringWithCount( win, filter_counts ), 'Select all the inbox files in the current view.', win._Select, file_filter_inbox )
            
            ClientGUIMenus.AppendMenuItem( select_menu, file_filter_archive.ToStringWithCount( win, filter_counts ), 'Select all the archived files in the current view.', win._Select, file_filter_archive )
            
        
        if len( all_specific_file_domains ) > 1:
            
            ClientGUIMenus.AppendSeparator( select_menu )
            
            all_specific_file_domains = ClientLocation.SortFileServiceKeysNicely( all_specific_file_domains )
            
            all_specific_file_domains = ClientLocation.FilterOutRedundantMetaServices( all_specific_file_domains )
            
            for file_service_key in all_specific_file_domains:
                
                file_filter = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_FILE_SERVICE, file_service_key )
                
                ClientGUIMenus.AppendMenuItem( select_menu, file_filter.ToStringWithCount( win, filter_counts ), 'Select all the files in this file domain.', win._Select, file_filter )
                
            
        
        if has_local_and_remote:
            
            file_filter_local = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_LOCAL )
            file_filter_remote = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_REMOTE )
            
            ClientGUIMenus.AppendSeparator( select_menu )
            
            ClientGUIMenus.AppendMenuItem( select_menu, file_filter_local.ToStringWithCount( win, filter_counts ), 'Select all the files that are in this client.', win._Select, file_filter_local )
            ClientGUIMenus.AppendMenuItem( select_menu, file_filter_remote.ToStringWithCount( win, filter_counts ), 'Select all the files that are not in this client.', win._Select, file_filter_remote )
            
        
        file_filter_selected = ClientMediaFileFilter.FileFilter( ClientMediaFileFilter.FILE_FILTER_SELECTED )
        selected_count = file_filter_selected.GetCount( win, filter_counts )
        
        not_selected_count = file_filter_not_selected.GetCount( win, filter_counts )
        
        if selected_count > 0:
            
            if not_selected_count > 0:
                
                ClientGUIMenus.AppendSeparator( select_menu )
                
                ClientGUIMenus.AppendMenuItem( select_menu, file_filter_not_selected.ToStringWithCount( win, filter_counts ), 'Swap what is and is not selected.', win._Select, file_filter_not_selected )
                
            
            ClientGUIMenus.AppendSeparator( select_menu )
            
            ClientGUIMenus.AppendMenuItem( select_menu, file_filter_none.ToStringWithCount( win, filter_counts ), 'Deselect everything selected.', win._Select, file_filter_none )
            
        
        ClientGUIMenus.AppendMenu( menu, select_menu, 'select' )
        
    
class Selectable( object ):
    
    def __init__( self ): self._selected = False
    
    def Deselect( self ): self._selected = False
    
    def IsSelected( self ): return self._selected
    
    def Select( self ): self._selected = True
    
class Thumbnail( Selectable ):
    
    def __init__( self ):
        
        Selectable.__init__( self )
        
        self._last_tags = None
        
        self._last_upper_summary = None
        self._last_lower_summary = None
        
    
    def ClearTagSummaryCaches( self ):
        
        self._last_tags = None
        
        self._last_upper_summary = None
        self._last_lower_summary = None
        
    
    def GetQtImage( self, device_pixel_ratio ) -> QG.QImage:
        
        # we probably don't really want to say DPR as a param here, but instead ask for a qt_image in a certain resolution?
        # or just give the qt_image to be drawn to?
        # or just give a painter and a rect and draw to that or something
        # we don't really want to mess around with DPR here, we just want to draw thumbs
        # that said, this works after a medium-high headache getting it there, so let's not get ahead of ourselves
        
        thumbnail_hydrus_bmp = CG.client_controller.GetCache( 'thumbnail' ).GetThumbnail( self )
        
        thumbnail_border = CG.client_controller.new_options.GetInteger( 'thumbnail_border' )
        
        ( width, height ) = ClientData.AddPaddingToDimensions( HC.options[ 'thumbnail_dimensions' ], thumbnail_border * 2 )
        
        qt_image_width = int( width * device_pixel_ratio )
        
        qt_image_height = int( height * device_pixel_ratio )
        
        qt_image = CG.client_controller.bitmap_manager.GetQtImage( qt_image_width, qt_image_height, 24 )
        
        qt_image.setDevicePixelRatio( device_pixel_ratio )
        
        inbox = self.HasInbox()
        
        local = self.GetLocationsManager().IsLocal()
        
        #
        # BAD FONT QUALITY AT 100% UI Scale (semi fixed now, look at the bottom)
        #
        # Ok I have spent hours on this now trying to figure it out and can't, so I'll just write about it for when I come back
        # So, if you boot with two monitors at 100% UI scale, the text here on a QImage is ugly, but on QWidget it is fine
        # If you boot with one monitor at 125%, the text is beautiful on QImage both screens
        # My current assumption is booting Qt with unusual UI scales triggers some extra init and that spills over to QImage QPainter initialisation
        #
        # I checked painter hints, font stuff, fontinfo and fontmetrics, and the only difference was with fontmetrics, on all-100% vs one >100%:
        # minLeftBearing: -1, -7
        # minRightBearing: -1, -8
        # xHeight: 3, 6
        #
        # The fontmetric produced a text size one pixel less wide on the both-100% run, so it is calculating different
        # However these differences are global to the program so don't explain why painting on a QImage specifically has bad font rather than QWidget
        # The ugly font is anti-aliased, but it looks like not drawn with sub-pixel calculations, like ClearType isn't kicking in or something
        # If I blow the font size up to 72, there is still a difference in screenshots between the all-100% and some >100% boot.
        # So, maybe if the program boots with any weird UI scale going on, Qt kicks in a different renderer for all QImages, the same renderer for QWidgets, perhaps more expensively
        # Or this is just some weird bug
        # Or I am still missing some flag
        #
        # bit like this https://stackoverflow.com/questions/31043332/qt-antialiasing-of-vertical-text-rendered-using-qpainter
        #
        # EDIT: OK, I 'fixed' it with setStyleStrategy( preferantialias ), which has no change in 125%, but in all-100% it draws something different but overall better quality
        # Note you can't setStyleStrategy on the font when it is in the QPainter. either it gets set read only or there is some other voodoo going on
        # It does look very slightly weird, but it is a step up so I won't complain. it really seems like the isolated QPainter of only-100% world has some different initialisation. it just can't find the nice font renderer
        #
        # EDIT 2: I think it may only look weird when the thumb banner has opacity. Maybe I need to learn about CompositionModes
        #
        # EDIT 3: Appalently Qt 6.4.0 may fix the basic 100% UI scale QImage init bug!
        #
        # UPDATE 3a: Qt 6.4.x did not magically fix it. It draws much nicer, but still a different font weight/metrics compared to media viewer background, say.
        # The PreferAntialias flag on 6.4.x seems to draw very very close to our ideal, so let's be happy with it for now.
        
        painter = QG.QPainter( qt_image )
        
        painter.setRenderHint( QG.QPainter.TextAntialiasing, True ) # is true already in tests, is supposed to be 'the way' to fix the ugly text issue
        painter.setRenderHint( QG.QPainter.Antialiasing, True ) # seems to do nothing, it only affects primitives?
        painter.setRenderHint( QG.QPainter.SmoothPixmapTransform, True ) # makes the thumb QImage scale up and down prettily when we need it, either because it is too small or DPR gubbins
        
        new_options = CG.client_controller.new_options
        
        if not local:
            
            if self._selected:
                
                background_colour_type = CC.COLOUR_THUMB_BACKGROUND_REMOTE_SELECTED
                
            else:
                
                background_colour_type = CC.COLOUR_THUMB_BACKGROUND_REMOTE
                
            
        else:
            
            if self._selected:
                
                background_colour_type = CC.COLOUR_THUMB_BACKGROUND_SELECTED
                
            else:
                
                background_colour_type = CC.COLOUR_THUMB_BACKGROUND
                
            
        
        # the painter isn't getting QSS style from the qt_image, we need to set the font explitly to get font size changes from QSS etc..
        
        f = QG.QFont( CG.client_controller.gui.font() )
        
        # this line magically fixes the bad text, as above
        f.setStyleStrategy( QG.QFont.PreferAntialias )
        
        painter.setFont( f )
        
        painter.fillRect( thumbnail_border, thumbnail_border, width - ( thumbnail_border * 2 ), height - ( thumbnail_border * 2 ), new_options.GetColour( background_colour_type ) )
        
        raw_thumbnail_qt_image = thumbnail_hydrus_bmp.GetQtImage()
        
        thumbnail_dpr_percent = CG.client_controller.new_options.GetInteger( 'thumbnail_dpr_percent' )
        
        if thumbnail_dpr_percent != 100:
            
            thumbnail_dpr = thumbnail_dpr_percent / 100
            
            raw_thumbnail_qt_image.setDevicePixelRatio( thumbnail_dpr )
            
            # qt_image.deviceIndepedentSize isn't supported in Qt5 lmao
            device_independent_thumb_size = raw_thumbnail_qt_image.size() / thumbnail_dpr
            
        else:
            
            device_independent_thumb_size = raw_thumbnail_qt_image.size()
            
        
        x_offset = ( width - device_independent_thumb_size.width() ) // 2
        
        y_offset = ( height - device_independent_thumb_size.height() ) // 2
        
        painter.drawImage( x_offset, y_offset, raw_thumbnail_qt_image )
        
        TEXT_BORDER = 1
        
        new_options = CG.client_controller.new_options
        
        tags = self.GetTagsManager().GetCurrentAndPending( CC.COMBINED_TAG_SERVICE_KEY, ClientTags.TAG_DISPLAY_SINGLE_MEDIA )
        
        if len( tags ) > 0:
            
            upper_tag_summary_generator = new_options.GetTagSummaryGenerator( 'thumbnail_top' )
            lower_tag_summary_generator = new_options.GetTagSummaryGenerator( 'thumbnail_bottom_right' )
            
            if self._last_tags is not None and self._last_tags == tags:
                
                upper_summary = self._last_upper_summary
                lower_summary = self._last_lower_summary
                
            else:
                
                upper_summary = upper_tag_summary_generator.GenerateSummary( tags )
                
                lower_summary = lower_tag_summary_generator.GenerateSummary( tags )
                
                self._last_tags = set( tags )
                
                self._last_upper_summary = upper_summary
                self._last_lower_summary = lower_summary
                
            
            if len( upper_summary ) > 0 or len( lower_summary ) > 0:
                
                if len( upper_summary ) > 0:
                    
                    text_colour_with_alpha = upper_tag_summary_generator.GetTextColour()
                    
                    background_colour_with_alpha = upper_tag_summary_generator.GetBackgroundColour()
                    
                    ( text_size, upper_summary ) = ClientGUIFunctions.GetTextSizeFromPainter( painter, upper_summary )
                    
                    box_x = thumbnail_border
                    box_y = thumbnail_border
                    box_width = width - ( thumbnail_border * 2 )
                    box_height = text_size.height() + 2
                    
                    painter.fillRect( box_x, box_y, box_width, box_height, background_colour_with_alpha )
                    
                    text_x = ( width - text_size.width() ) // 2
                    text_y = box_y + TEXT_BORDER
                    
                    painter.setPen( QG.QPen( text_colour_with_alpha ) )
                    
                    ClientGUIFunctions.DrawText( painter, text_x, text_y, upper_summary )
                    
                
                if len( lower_summary ) > 0:
                    
                    text_colour_with_alpha = lower_tag_summary_generator.GetTextColour()
                    
                    background_colour_with_alpha = lower_tag_summary_generator.GetBackgroundColour()
                    
                    ( text_size, lower_summary ) = ClientGUIFunctions.GetTextSizeFromPainter( painter, lower_summary )
                    
                    text_width = text_size.width()
                    text_height = text_size.height()
                    
                    box_width = text_width + ( TEXT_BORDER * 2 )
                    box_height = text_height + ( TEXT_BORDER * 2 )
                    box_x = width - box_width - thumbnail_border
                    box_y = height - text_height - thumbnail_border
                    
                    painter.fillRect( box_x, box_y, box_width, box_height, background_colour_with_alpha )
                    
                    text_x = box_x + TEXT_BORDER
                    text_y = box_y + TEXT_BORDER
                    
                    painter.setPen( QG.QPen( text_colour_with_alpha ) )
                    
                    ClientGUIFunctions.DrawText( painter, text_x, text_y, lower_summary )
                    
                
            
        
        if thumbnail_border > 0:
            
            if not local:
                
                if self._selected:
                    
                    border_colour_type = CC.COLOUR_THUMB_BORDER_REMOTE_SELECTED
                    
                else:
                    
                    border_colour_type = CC.COLOUR_THUMB_BORDER_REMOTE
                    
                
            else:
                
                if self._selected:
                    
                    border_colour_type = CC.COLOUR_THUMB_BORDER_SELECTED
                    
                else:
                    
                    border_colour_type = CC.COLOUR_THUMB_BORDER
                    
                
            
            # I had a hell of a time getting a transparent box to draw right with a pen border without crazy +1px in the params for reasons I did not understand
            # so I just decided four rects is neater and fine and actually prob faster in some cases
            
            #         _____            ______                              _____            ______      ________________
            # ___________(_)___  _________  /_______   _______ ______      __  /______      ___  /_________  /__  /__  /
            # ___  __ \_  /__  |/_/  _ \_  /__  ___/   __  __ `/  __ \     _  __/  __ \     __  __ \  _ \_  /__  /__  / 
            # __  /_/ /  / __>  < /  __/  / _(__  )    _  /_/ // /_/ /     / /_ / /_/ /     _  / / /  __/  / _  /  /_/  
            # _  .___//_/  /_/|_| \___//_/  /____/     _\__, / \____/      \__/ \____/      /_/ /_/\___//_/  /_/  (_)   
            # /_/                                      /____/                                                            
            
            painter.setBrush( QG.QBrush( new_options.GetColour( border_colour_type ) ) )
            painter.setPen( QG.QPen( QC.Qt.NoPen ) )
            
            rectangles = []
            
            side_height = height - ( thumbnail_border * 2 )
            rectangles.append( QC.QRectF( 0, 0, width, thumbnail_border ) ) # top
            rectangles.append( QC.QRectF( 0, height - thumbnail_border, width, thumbnail_border ) ) # bottom
            rectangles.append( QC.QRectF( 0, thumbnail_border, thumbnail_border, side_height ) ) # left
            rectangles.append( QC.QRectF( width - thumbnail_border, thumbnail_border, thumbnail_border, side_height ) ) # right
            
            painter.drawRects( rectangles )
            
        
        ICON_MARGIN = 1
        
        locations_manager = self.GetLocationsManager()
        
        icons_to_draw = []
        
        if locations_manager.IsDownloading():
            
            icons_to_draw.append( CC.global_pixmaps().downloading )
            
        
        if self.HasNotes():
            
            icons_to_draw.append( CC.global_pixmaps().notes )
            
        
        if locations_manager.IsTrashed() or CC.COMBINED_LOCAL_FILE_SERVICE_KEY in locations_manager.GetDeleted():
            
            icons_to_draw.append( CC.global_pixmaps().trash )
            
        
        if inbox:
            
            icons_to_draw.append( CC.global_pixmaps().inbox )
            
        
        if len( icons_to_draw ) > 0:
            
            icon_x = - ( thumbnail_border + ICON_MARGIN )
            
            for icon in icons_to_draw:
                
                icon_x -= icon.width()
                
                painter.drawPixmap( width + icon_x, thumbnail_border, icon )
                
                icon_x -= 2 * ICON_MARGIN
                
            
        
        if self.IsCollection():
            
            icon = CC.global_pixmaps().collection
            
            icon_x = thumbnail_border + ICON_MARGIN
            icon_y = ( height - 1 ) - thumbnail_border - ICON_MARGIN - icon.height()
            
            painter.drawPixmap( icon_x, icon_y, icon )
            
            num_files_str = HydrusData.ToHumanInt( self.GetNumFiles() )
            
            ( text_size, num_files_str ) = ClientGUIFunctions.GetTextSizeFromPainter( painter, num_files_str )
            
            text_width = text_size.width()
            text_height = text_size.height()
            
            box_width = text_width + ( ICON_MARGIN * 2 )
            box_x = icon_x + icon.width() + ICON_MARGIN
            box_height = text_height + ( ICON_MARGIN * 2 )
            box_y = ( height - 1 ) - box_height
            
            painter.fillRect( box_x, height - text_height - 3, box_width, box_height, CC.COLOUR_UNSELECTED )
            
            painter.setPen( QG.QPen( CC.COLOUR_SELECTED_DARK ) )
            
            text_x = box_x + ICON_MARGIN
            text_y = box_y + ICON_MARGIN
            
            ClientGUIFunctions.DrawText( painter, text_x, text_y, num_files_str )
            
        
        # top left icons
        
        icons_to_draw = []
        
        if self.HasAudio():
            
            icons_to_draw.append( CC.global_pixmaps().sound )
            
        elif self.HasDuration():
            
            icons_to_draw.append( CC.global_pixmaps().play )
            
        
        services_manager = CG.client_controller.services_manager
        
        remote_file_service_keys = CG.client_controller.services_manager.GetRemoteFileServiceKeys()
        
        current = locations_manager.GetCurrent().intersection( remote_file_service_keys )
        pending = locations_manager.GetPending().intersection( remote_file_service_keys )
        petitioned = locations_manager.GetPetitioned().intersection( remote_file_service_keys )
        
        current_to_display = current.difference( petitioned )
        
        #
        
        service_types = [ services_manager.GetService( service_key ).GetServiceType() for service_key in current_to_display ]
        
        if HC.FILE_REPOSITORY in service_types:
            
            icons_to_draw.append( CC.global_pixmaps().file_repository )
            
        
        if HC.IPFS in service_types:
            
            icons_to_draw.append( CC.global_pixmaps().ipfs )
            
        
        #
        
        service_types = [ services_manager.GetService( service_key ).GetServiceType() for service_key in pending ]
        
        if HC.FILE_REPOSITORY in service_types:
            
            icons_to_draw.append( CC.global_pixmaps().file_repository_pending )
            
        
        if HC.IPFS in service_types:
            
            icons_to_draw.append( CC.global_pixmaps().ipfs_pending )
            
        
        #
        
        service_types = [ services_manager.GetService( service_key ).GetServiceType() for service_key in petitioned ]
        
        if HC.FILE_REPOSITORY in service_types:
            
            icons_to_draw.append( CC.global_pixmaps().file_repository_petitioned )
            
        
        if HC.IPFS in service_types:
            
            icons_to_draw.append( CC.global_pixmaps().ipfs_petitioned )
            
        
        top_left_x = thumbnail_border + ICON_MARGIN
        
        for icon_to_draw in icons_to_draw:
            
            painter.drawPixmap( top_left_x, thumbnail_border + ICON_MARGIN, icon_to_draw )
            
            top_left_x += icon_to_draw.width() + ( ICON_MARGIN * 2 )
            
        
        return qt_image
        
    
class ThumbnailMediaCollection( Thumbnail, ClientMedia.MediaCollection ):
    
    def __init__( self, location_context, media_results ):
        
        ClientMedia.MediaCollection.__init__( self, location_context, media_results )
        Thumbnail.__init__( self )
        
    
class ThumbnailMediaSingleton( Thumbnail, ClientMedia.MediaSingleton ):
    
    def __init__( self, media_result ):
        
        ClientMedia.MediaSingleton.__init__( self, media_result )
        Thumbnail.__init__( self )
        
    
