from transitions import Machine
from workflow.monitor import FilePatternMonitor
from workflow.utilities import (safe_copy_file, compare_hashes, compress_file,
                                uncompress_file, stack_files, globus_transfer,
                                create_scipion_project, start_scipion_project,
                                convert_to_mrc)
import asyncio
import logging
import os
import pathlib
import time

GLOBUS_ROOT = '/mnt/NCEF-CryoEM/'
ATC_GLOBUS_ENDPOINT = '67dace28-311f-11e8-b8f8-0ac6873fc732'
MOAB_GLOBUS_ENDPOINT = 'dabdccc3-6d04-11e5-ba46-22000b92c6ec'
logger = logging.getLogger(__name__)


class Project():
    '''Overarching project controller
    '''

    def __init__(self, project, pattern, frames=1, scipion_config=None,
                 globus_root=None):
        self.project = project
        self.workflow = Workflow()
        self.awh = AsyncWorkflowHelper()
        self.monitor = FilePatternMonitor(pattern, recursive=True)
        if globus_root is None:
            globus_root = GLOBUS_ROOT
        self.paths = {
                'local_root': '/tmp/' + str(project),
                'storage_root': '/mnt/nas/' + str(project),
                'globus_root': globus_root.rstrip('/') + '/' + str(project),
                'scipion_config': scipion_config
                }
        self._ensure_root_directories()
        self.frames = frames
        if self.frames > 1:
            self._ensure_directory(str(
                pathlib.Path(self.paths['local_root']).joinpath(
                    pathlib.Path('stack'))))
        self.workflow.MIN_IMPORT_INTERVAL = \
            self.workflow.MIN_IMPORT_INTERVAL / self.frames

    def start(self):
        self._transfer_loop()
        self.awh.add_timed_callback(self._start_scipion, 60)
        self.awh.loop.run_until_complete(self._async_start())

    async def _async_start(self):
        try:
            while True:
                items = await self.monitor
                for item in items:
                    model = WorkflowItem(item, self.workflow, self)
                    self.workflow.add_model(model)
                    model.initialize()
                    await asyncio.sleep(self.workflow.MIN_IMPORT_INTERVAL)
                await asyncio.sleep(2)
        except StopAsyncIteration:
            import sys
            sys.exit(0)

    def _start_scipion(self):
        if not self.paths['scipion_config']:
            logger.info('Not starting Scipion, no config file found')
            return None
        else:
            logger.info('Starting Scipion for {0}'.format(self.project))
        self.awh.create_task(
            create_scipion_project(self.project,
                                   self.paths['scipion_config']),
            done_cb=self._schedule_scipion_project)

    def _schedule_scipion_project(self, fut=None):
        if fut.exception():
            logger.warning('Could not create scipion project. {0}'
                           .format(fut.exception()))
        else:
            self.awh.create_task(start_scipion_project(self.project))

    def _transfer_loop(self, fut=None):
        self.awh.create_task(
            self._schedule_globus_transfer(),
            done_cb=self._transfer_loop
        )

    async def _schedule_globus_transfer(self, pre_wait=1800):
        '''Schedule the globus transfer for synchronization after a wait time

        Default pre_wait (seconds) is 1800. Decrease to 0 for one-offs or if
        you're handling an inter-call interval yourself.
        '''
        if not self.project:
            raise KeyError('Project name must not be empty.')
        await asyncio.sleep(pre_wait)
        await globus_transfer(
            ATC_GLOBUS_ENDPOINT + ':/' + str(self.project),
            MOAB_GLOBUS_ENDPOINT + ':' + self.paths['globus_root'],
            '-s', 'mtime',
            '-r',
            '--preserve-mtime',
            '--notify', 'failed,inactive',
            '--label', str(self.project))

    def _ensure_root_directories(self):
        self._ensure_directory(self.paths['local_root'])
        self._ensure_directory(self.paths['storage_root'])

    @staticmethod
    def _ensure_directory(path):
        try:
            os.stat(str(path))
        except FileNotFoundError:
            os.makedirs(str(path), exist_ok=True)


class Workflow(Machine):
    '''The workflow state machine.
    '''
    MIN_IMPORT_INTERVAL = 45

    def __init__(self):
        states = ['initial',
                  'creating',
                  'importing',
                  'converting',
                  'stacking',
                  'compressing',
                  'exporting',
                  'processing',
                  'confirming',
                  'cleaning',
                  'finished']
        Machine.__init__(self,
                         states=states,
                         initial='initial',
                         auto_transitions=False)
        self.add_transition('initialize', source='initial', dest='creating')
        self.add_transition('import_file',
                            source=['creating', 'importing'],
                            dest='importing')
        self.add_transition('stack',
                            source=['importing', 'stacking'],
                            dest='stacking')
        self.add_transition('compress',
                            source=['importing', 'stacking', 'compressing',
                                    'converting'],
                            dest='compressing')
        self.add_transition('export',
                            source=['compressing', 'exporting'],
                            dest='exporting')
        self.add_transition('convert_to_mrc',
                            source=['converting', 'importing'],
                            dest='converting')
        self.add_transition('hold_for_processing',
                            source=['exporting', 'processing'],
                            dest='processing')
        self.add_transition('confirm',
                            source=['processing', 'exporting'],
                            dest='confirming')
        self.add_transition('clean',
                            source=['stacking', 'confirming'],
                            dest='cleaning')
        self.add_transition('finalize', source='cleaning', dest='finished')

    def get_model(self, key):
        models = [model for model in self.models[1:]
                  if model.files['original'] == key]
        if models:
            return models[0]
        else:
            raise KeyError


class WorkflowItem():
    '''A file that will join and proceed through the workflow.
    '''

    def __init__(self, path, workflow, project):
        self.history = []
        self.files = {'original': pathlib.Path(path)}
        self.project = project
        self.workflow = workflow
        self.awh = project.awh
        logger.info('Starting: {0}'.format(self.files['original']))

    def _delta_mtime(self, path):
        '''Return the difference between system time and file modified timestamp
        '''
        return int(time.time()) - os.stat(str(path)).st_mtime

    def _is_processing_complete(self, path):
        project_index = pathlib.Path(
            '/var/www/scipion/',
            self.project.project,
            'index.html')
        try:
            with open(str(project_index), mode='r', encoding='utf8') as index:
                if str(self.files['original'].stem) in index.read():
                    return True
                else:
                    return False
        except FileNotFoundError:
            return False

    def on_enter_creating(self):
        '''Check that the file has finished creation, then transition state

        Since we're using network file systems here, we're using a simple check
        to see if the file has been modified recently instead of something
        fancier like inotify.
        '''
        dt = self._delta_mtime(self.files['original'])
        self.import_file() if dt > 15 else self.awh.add_timed_callback(
                self.on_enter_creating, 16 - dt)

    def on_enter_importing(self):
        '''Copy (import) the file to local storage for processing.
        '''
        self.files['local_original'] = pathlib.Path(
                self.project.paths['local_root'],
                self.files['original'].name)
        self.awh.create_task(
            safe_copy_file(self.files['original'],
                           self.files['local_original']),
            self._importing_complete)

    def _importing_complete(self, fut):
        if fut.exception():
            self.awh.add_timed_callback(self.import_file, 10)
        elif fut.result() == 0:
            if self.project.frames > 1:
                self.stack()
            elif self.files['original'].suffix == '.dm4':
                self.convert_to_mrc()
            else:
                self.files['local_stack'] = self.files['local_original']
                self.compress()
        else:
            self.awh.add_timed_callback(self.import_file, 10)

    def on_enter_converting(self):
        self.files['local_converted'] = \
            self.files['local_original'].with_suffix('.mrc')
        self.awh.create_task(
            convert_to_mrc(self.files['local_original'],
                           self.files['local_converted']),
            self._converting_complete)

    def _converting_complete(self, fut):
        if fut.exception():
            self.awh.add_timed_callback(self.convert_to_mrc, 10)
        else:
            self.files['local_stack'] = self.files['local_original']
            self.compress()

    def on_enter_stacking(self):
        '''Stack the files if the stack parameter evaluates True.

        If the file is an unstacked frame, check to see if there is a workflow
        item for the stack created. If there's already a workflow item,
        reference this file in that item's self.files['local_unstacked'].

        If the file is a stacked movie placeholder, call out and back until
        all of the frames are referenced, then perform stacking. If that is
        successful, trigger clean-up for each of the frames and move to
        compressing.
        '''
        if self.project.frames == 1:
            self.compress()
            return
        if ('local_unstacked' in self.files and
                len(self.files['local_unstacked']) == self.project.frames):
            pths = [f.files['original'] for f in self.files['local_unstacked']]
            self.awh.create_task(stack_files(pths,
                                             self.files['original']),
                                 done_cb=self._stacking_complete)
        elif 'local_unstacked' not in self.files:
            stack_key = self.files['local_original'].stem[:-2] +\
                self.files['local_original'].suffix
            stack_path = self.files['local_original'].parent.joinpath(
                pathlib.Path('stack')).joinpath(
                pathlib.Path(stack_key))
            model = WorkflowItem(stack_path, self.workflow, self.project)
            model.files['local_original'] = stack_path
            model.files['local_stack'] = model.files['local_original']
            try:
                model = self.workflow.get_model(stack_path)
            except KeyError:
                self.workflow.add_model(model, initial='stacking')
            try:
                model.files['local_unstacked'].append(self)
            except KeyError:
                model.files['local_unstacked'] = [self]
            model.stack()
        else:
            pass
            # Only stack models before hitting the frame count should get here

    def _stacking_complete(self, fut):
        if not fut.exception():
            self.compress()
        else:
            pass

    def on_enter_compressing(self):
        '''Trigger compression of the local stack file.

        The files are large, so compression is ideally multithreaded. The
        compression function should call back when complete to trigger
        the move to the next state.
        '''
        self.awh.create_task(
            compress_file(self.files['local_stack'], force=True),
            done_cb=self._compressing_complete)
        self.files['local_compressed'] = self.files['local_stack'].with_suffix(
            self.files['local_stack'].suffix + '.bz2')

    def _compressing_complete(self, fut):
        self.export() if not fut.exception() else self.compress()

    def on_enter_exporting(self):
        '''Export (copy) the compressed file to the storage location
        '''
        self.files['storage_final'] = pathlib.Path(
            self.project.paths['storage_root'],
            self.files['local_compressed'].name)
        self.awh.create_task(
            safe_copy_file(
                self.files['local_compressed'],
                self.files['storage_final']),
            self._exporting_complete)

    def _exporting_complete(self, fut):
        if fut.exception():
            self.awh.add_timed_callback(self.export, 10)
        elif fut.result() == 0:
            self.hold_for_processing()
        else:
            self.awh.add_timed_callback(self.export, 10)

    def on_enter_processing(self):
        '''Maintain processing state until scipion processing is complete.

        Watch for the indicators that the entire scipion processing stack has
        completed. Until then, recurse back to this state entrance. Once it has
        completed, proceed to clean up.
        '''
        if self._is_processing_complete(self.files['local_stack']):
            self.confirm()
        else:
            self.awh.add_timed_callback(self.hold_for_processing, 10)

    def on_enter_confirming(self):
        '''Verify compression and that storage transfer is complete

        Confirm that:
        - The compression cycle is correct (hash original and re-uncompressed)
        - The transfer to storage is complete
        '''
        new_name = self.files['local_original'].with_suffix('.orig')
        self.files['local_uncompressed'] = self.files['local_original']
        self.files['local_original'].rename(new_name)
        self.files['local_original'] = pathlib.Path(new_name)
        self.awh.create_task(
            uncompress_file(self.files['local_compressed'], force=True),
            self._uncompress_complete)

    def _uncompress_complete(self, fut):
        size_match = (os.stat(str(self.files['local_compressed'])).st_size ==
                      os.stat(str(self.files['storage_final'])).st_size)
        if size_match:
            self.awh.create_task(
                compare_hashes(
                    self.files['local_original'],
                    self.files['local_uncompressed']),
                self._hashes_complete)
        else:
            pass

    def _hashes_complete(self, fut):
        if fut.exception():
            self.awh.add_timed_callback(
                self._uncompress_complete, 10)
            logger.warning(fut.exception())
        elif fut.result() is True:
            self._confirm_complete(fut)
        else:
            pass

    def _confirm_complete(self, fut):
        self.clean()

    def on_enter_cleaning(self):
        self._safe_remove_file('local_stack')
        self._safe_remove_file('local_compressed')
        self._safe_remove_file('local_uncompressed')
        self._safe_remove_file('local_original')
        self._safe_remove_file('local_converted')
        self._safe_remove_file('original')
        if 'local_unstacked' in self.files:
            [x.clean() for x in self.files['local_unstacked']]
        self.finalize()

    def _safe_remove_file(self, key):
        try:
            self._remove_file(self.files[key])
        except KeyError:
            pass

    def _remove_file(self, path):
        try:
            os.remove(str(path))
        except OSError:
            pass

    def on_enter_finished(self):
        logger.info('Finalized: {0}'.format(self.files['original']))


class AsyncWorkflowHelper():
    '''Processes async calls for the workflow
    '''

    def __init__(self):
        self.loop = asyncio.get_event_loop()

    def create_task(self, coro, done_cb=None):
        task = self.loop.create_task(coro)
        task.add_done_callback(done_cb) if done_cb else None

    def add_timed_callback(self, func, sleep):
        self.loop.create_task(self._wrap_timed_callback(func, sleep))

    async def _wrap_timed_callback(self, func, sleep):
        await asyncio.sleep(sleep)
        func()
