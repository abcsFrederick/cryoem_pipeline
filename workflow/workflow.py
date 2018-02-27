from transitions import Machine
from workflow.monitor import FilePatternMonitor
from workflow.utilities import safe_copy_file, compress_file, stack_files
import asyncio
import os
import pathlib
import time


class Project():
    '''Overarching project controller
    '''

    def __init__(self, project, pattern, frames=1):
        self.project = project
        self.workflow = Workflow()
        self.async = AsyncWorkflowHelper()
        self.monitor = FilePatternMonitor(pattern)
        self.paths = {
                'local_root': '/tmp/' + str(project),
                'storage_root': '/mnt/moab/' + str(project)
                }
        self.frames = frames

    def start(self):
        self.async.loop.run_until_complete(self._async_start())

    async def _async_start(self):
        while True:
            items = await self.monitor
            for item in items:
                model = WorkflowItem(item, self.workflow, self.project)
                self.workflow.add_model(model)
                model.initialize()
            await asyncio.sleep(self.workflow.MIN_IMPORT_INTERVAL)


class Workflow(Machine):
    '''The workflow state machine.
    '''
    MIN_IMPORT_INTERVAL = 20

    def __init__(self):
        states = ['initial',
                  'creating',
                  'importing',
                  'stacking',
                  'compressing',
                  'exporting',
                  'processing',
                  'cleaning',
                  'finished']
        Machine.__init__(self,
                         states=states,
                         initial='initial',
                         auto_transitions=False)
        self.add_transition('initialize', source='initial', dest='creating')
        self.add_transition('import_file', source='creating', dest='importing')
        self.add_transition('stack', source=['importing', 'stacking'],
                            dest='stacking')
        self.add_transition('compress', source=['importing', 'stacking'],
                            dest='compressing')
        self.add_transition('export', source='compressing', dest='exporting')
        self.add_transition('hold_for_processing', source='exporting',
                            dest='processing')
        self.add_transition('clean', source=['processing', 'exporting'],
                            dest='cleaning')
        self.add_transition('finalize', source='cleaning', dest='finished')

    def get_model(self, key):
        models = [model for model in self.models
                  if model.files['original'] == key]
        return models[0]


class WorkflowItem():
    '''A file that will join and proceed through the workflow.
    '''

    def __init__(self, path, workflow, project):
        self.history = []
        self.files = {'original': pathlib.Path(path)}
        self.project = project
        self.workflow = workflow

    def _delta_mtime(self, path):
        '''Return the difference between system time and file modified timestamp
        '''
        return int(time.time()) - os.stat(path).st_mtime

    def _is_processing_complete(self, path):
        raise NotImplementedError

    def on_enter_creating(self):
        '''Check that the file has finished creation, then transition state

        Since we're using network file systems here, we're using a simple check
        to see if the file has been modified recently instead of something
        fancier like inotify.
        '''
        dt = self._delta_mtime(self.path)
        self.import_file() if dt > 15 else self.async.add_timed_callback(
                self.on_enter_creating, 16 - dt)

    def on_enter_importing(self):
        '''Copy (import) the file to local storage for processing.
        '''
        self.files['local_original'] = pathlib.Path(
                self.project.paths['local_root'],
                self.files['original'].name)
        safe_copy_file(self.files['original'],
                       self.files['local_original'])
        if self.project.frames > 1:
            self.stack()
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
        if ('local_unstacked' in self.files.keys() and
                len(self.files['local_unstacked']) == self.project.frames):
            self.async.create_task(stack_files(self.files['local_unstacked'],
                                               self.files['original']),
                                   done_cb=self._stacking_complete)
        else:
            stack_key = self.files['local_original'].name[:-2]
            stack_path = self.files['local_original'].with_name(stack_key)
            model = WorkflowItem(stack_path, self.workflow, self.project)
            try:
                model = self.workflow.get_model(stack_key)
            except KeyError:
                self.workflow.add_model(model, initial='stacking')
            try:
                model.files['local_unstacked'].append(self)
            except KeyError:
                model.files['local_unstacked'] = [self]
            model.stack()

    def _stacking_complete(self, fut):
        if fut.result() == 0:
            [x.clean() for x in self.files['local_unstacked']]
            del self.files['local_unstacked']
            self.compress()
        else:
            pass

    def on_enter_compressing(self):
        '''Trigger compression of the local stack file.

        The files are large, so compression is ideally multithreaded. The
        compression function should call back when complete to trigger
        the move to the next state.
        '''
        self.async.create_task(compress_file(self.files['local_stack']),
                               done_cb=self._compressing_cb)
        self.files['local_compressed'] = self.files['local_stack'].with_suffix(
                self.files['local_stack'].suffix + '.bz2')

    def _compressing_cb(self, fut):
        self.export() if not fut.exception() else self.compress()

    def on_enter_exporting(self):
        '''Export (copy) the compressed file to the storage location
        '''
        self.files['storage_final'] = pathlib.Path(
                self.project.paths['storage_root'],
                self.files['local_compressed'])
        safe_copy_file(self.files['local_compressed'],
                       self.files['storage_final'])
        self.hold_for_processing()

    def on_enter_processing(self):
        '''Maintain processing state until scipion processing is complete.

        Watch for the indicators that the entire scipion processing stack has
        completed. Until then, recurse back to this state entrance. Once it has
        completed, proceed to clean up.
        '''
        if self._is_processing_complete(self.files['local_stack']):
            self.clean()
        else:
            self.async.add_timed_callback(self.on_enter_processing, 10)

    def on_enter_cleaning(self):
        raise NotImplementedError

    def on_enter_finished(self):
        raise NotImplementedError


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
