# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import shutil
import threading
import time
from abc import ABC, abstractmethod
from threading import Lock
from typing import List, Optional

from nvflare.apis.event_type import EventType
from nvflare.apis.fl_component import FLComponent
from nvflare.apis.fl_constant import (
    FLContextKey,
    MachineStatus,
    RunProcessKey,
    ServerCommandKey,
    ServerCommandNames,
    SnapshotKey,
    WorkspaceConstants,
)
from nvflare.apis.fl_context import FLContext
from nvflare.apis.shareable import Shareable
from nvflare.apis.workspace import Workspace
from nvflare.fuel.f3.cellnet.cell import Cell, Message
from nvflare.fuel.f3.cellnet.cell import make_reply as make_cellnet_reply
from nvflare.fuel.f3.cellnet.defs import MessageHeaderKey
from nvflare.fuel.f3.cellnet.defs import ReturnCode as F3ReturnCode
from nvflare.fuel.f3.cellnet.fqcn import FQCN
from nvflare.fuel.utils import fobs
from nvflare.fuel.utils.argument_utils import parse_vars
from nvflare.fuel.utils.zip_utils import unzip_all_from_bytes
from nvflare.private.defs import CellChannel, CellChannelTopic, CellMessageHeaderKeys, new_cell_message
from nvflare.private.fed.server.server_runner import ServerRunner
from nvflare.widgets.fed_event import ServerFedEventRunner

from .client_manager import ClientManager
from .run_manager import RunManager
from .server_engine import ServerEngine
from .server_state import (
    ABORT_RUN,
    ACTION,
    MESSAGE,
    NIS,
    Cold2HotState,
    ColdState,
    Hot2ColdState,
    HotState,
    ServerState,
)
from .server_status import ServerStatus


class BaseServer(ABC):
    def __init__(
        self,
        project_name=None,
        min_num_clients=2,
        max_num_clients=10,
        heart_beat_timeout=600,
        handlers: Optional[List[FLComponent]] = None,
    ):
        """Base server that provides the clients management and server deployment."""
        self.project_name = project_name
        self.min_num_clients = max(min_num_clients, 1)
        self.max_num_clients = max(max_num_clients, 1)

        self.heart_beat_timeout = heart_beat_timeout
        self.handlers = handlers
        # self.cmd_modules = cmd_modules

        self.client_manager = ClientManager(
            project_name=self.project_name, min_num_clients=self.min_num_clients, max_num_clients=self.max_num_clients
        )

        # self.grpc_server = None
        self.cell = None
        self.admin_server = None
        self.lock = Lock()
        self.snapshot_lock = Lock()
        self.fl_ctx = FLContext()
        self.platform = None

        self.shutdown = False
        self.status = ServerStatus.NOT_STARTED

        self.abort_signal = None
        self.executor = None

        self.logger = logging.getLogger(self.__class__.__name__)

    def get_all_clients(self):
        return self.client_manager.get_clients()

    @abstractmethod
    def remove_client_data(self, token):
        pass

    def close(self):
        """Shutdown the server."""
        try:
            if self.lock:
                self.lock.release()
        except RuntimeError:
            self.logger.info("canceling sync locks")
        try:
            if self.cell:
                self.cell.stop()
        finally:
            self.logger.info("server off")
            return 0

    def deploy(self, args, grpc_args=None, secure_train=False):
        """Start a grpc server and listening the designated port."""
        # num_server_workers = grpc_args.get("num_server_workers", 1)
        # num_server_workers = max(self.client_manager.get_min_clients(), num_server_workers)
        target = grpc_args["service"].get("target", "0.0.0.0:6007")
        scheme = grpc_args["service"].get("scheme", "http://")

        # grpc_options = grpc_args["service"].get("options", GRPC_DEFAULT_OPTIONS)
        credentials = {}
        parent_url = None

        my_fqcn = FQCN.ROOT_SERVER
        self.cell = Cell(
            fqcn=my_fqcn,
            root_url=scheme + target,
            secure=secure_train,
            credentials=credentials,
            create_internal_listener=True,
            parent_url=parent_url,
        )

        self.cell.start()

    def client_cleanup(self):
        while not self.shutdown:
            self.remove_dead_clients()
            time.sleep(15)

    def set_admin_server(self, admin_server):
        self.admin_server = admin_server

    def remove_dead_clients(self):
        # Clean and remove the dead client without heartbeat.
        self.logger.debug("trying to remove dead clients .......")
        delete = []
        for token, client in self.client_manager.get_clients().items():
            if client.last_connect_time < time.time() - self.heart_beat_timeout:
                delete.append(token)
        for token in delete:
            client = self.logout_client(token)
            self.logger.info(
                "Remove the dead Client. Name: {}\t Token: {}.  Total clients: {}".format(
                    client.name, token, len(self.client_manager.get_clients())
                )
            )

    def logout_client(self, token):
        client = self.client_manager.remove_client(token)
        self.remove_client_data(token)
        if self.admin_server:
            self.admin_server.client_dead(token)
        self.notify_dead_client(client)
        return client

    def notify_dead_client(self, client):
        """Called to do further processing of the dead client

        Args:
            client: the dead client

        Returns:

        """
        pass

    def fl_shutdown(self):
        self.shutdown = True
        self.close()
        if self.executor:
            self.executor.shutdown()


class FederatedServer(BaseServer):
    def __init__(
        self,
        project_name=None,
        min_num_clients=2,
        max_num_clients=10,
        cmd_modules=None,
        heart_beat_timeout=600,
        handlers: Optional[List[FLComponent]] = None,
        args=None,
        secure_train=False,
        snapshot_persistor=None,
        overseer_agent=None,
    ):
        """Federated server services.

        Args:
            project_name: server project name.
            min_num_clients: minimum number of contributors at each round.
            max_num_clients: maximum number of contributors at each round.
            cmd_modules: command modules.
            heart_beat_timeout: heartbeat timeout
            handlers: A list of handler
            args: arguments
            secure_train: whether to use secure communication
        """
        BaseServer.__init__(
            self,
            project_name=project_name,
            min_num_clients=min_num_clients,
            max_num_clients=max_num_clients,
            heart_beat_timeout=heart_beat_timeout,
            handlers=handlers,
        )

        self.contributed_clients = {}
        self.tokens = None
        self.round_started = time.time()

        with self.lock:
            self.reset_tokens()

        self.cmd_modules = cmd_modules

        self.builder = None

        # Additional fields for CurrentTask meta_data in GetModel API.
        self.current_model_meta_data = {}

        self.engine = self._create_server_engine(args, snapshot_persistor)
        self.run_manager = None
        self.server_runner = None

        self.processors = {}
        self.runner_config = None
        self.secure_train = secure_train

        self.workspace = args.workspace
        self.snapshot_location = None
        self.overseer_agent = overseer_agent
        self.server_state: ServerState = ColdState()
        self.snapshot_persistor = snapshot_persistor

        # self._register_cellnet_cbs()

    def _register_cellnet_cbs(self):
        self.cell.register_request_cb(
            channel=CellChannel.TASK,
            topic=CellChannelTopic.Register,
            cb=self.register_client,
        )
        self.cell.register_request_cb(
            channel=CellChannel.TASK,
            topic=CellChannelTopic.Quit,
            cb=self.quit_client,
        )
        self.cell.register_request_cb(
            channel=CellChannel.TASK,
            topic=CellChannelTopic.HEART_BEAT,
            cb=self.client_heartbeat,
        )

        self.cell.register_request_cb(
            channel=CellChannel.SERVER_PARENT_LISTENER,
            topic="*",
            cb=self._listen_command,
        )

    def _listen_command(self, request: Message) -> Message:

        assert isinstance(request, Message), "request must be CellMessage but got {}".format(type(request))

        job_id = request.get_header(CellMessageHeaderKeys.JOB_ID)
        command = request.get_header(MessageHeaderKey.TOPIC)
        data = fobs.loads(request.payload)

        if command == ServerCommandNames.GET_CLIENTS:
            if job_id in self.engine.run_processes:
                clients = self.engine.run_processes.get(job_id).get(RunProcessKey.PARTICIPANTS)
                # job_id = self.engine.run_processes.get(job_id).get(RunProcessKey.JOB_ID)
                return_data = {ServerCommandKey.CLIENTS: clients, ServerCommandKey.JOB_ID: job_id}
            else:
                return_data = {ServerCommandKey.CLIENTS: None, ServerCommandKey.JOB_ID: job_id}

            return make_cellnet_reply(F3ReturnCode.OK, "", fobs.dumps(return_data))
        elif command == ServerCommandNames.UPDATE_RUN_STATUS:
            execution_error = data.get("execution_error")
            if execution_error:
                with self.lock:
                    run_process_info = self.engine.run_processes.get(job_id)
                    self.engine.exception_run_processes[job_id] = run_process_info
                    reply = make_cellnet_reply(F3ReturnCode.OK, "", None)
                    return reply
        elif command == ServerCommandNames.HEARTBEAT:
            return make_cellnet_reply(F3ReturnCode.OK, "", None)
        else:
            return make_cellnet_reply(F3ReturnCode.INVALID_REQUEST, "", None)

    def _create_server_engine(self, args, snapshot_persistor):
        return ServerEngine(
            server=self, args=args, client_manager=self.client_manager, snapshot_persistor=snapshot_persistor
        )

    # @property
    def task_meta_info(self, client_name):
        """Task meta information.

        The model_meta_info uniquely defines the current model,
        it is used to reject outdated client's update.
        """
        meta_info = {
            CellMessageHeaderKeys.PROJECT_NAME: self.project_name,
            CellMessageHeaderKeys.CLIENT_NAME: client_name,
        }
        return meta_info

    def remove_client_data(self, token):
        self.tokens.pop(token, None)

    def reset_tokens(self):
        """Reset the token set.

        After resetting, each client can take a token
        and start fetching the current global model.
        This function is not thread-safe.
        """
        self.tokens = dict()
        for client in self.get_all_clients().keys():
            self.tokens[client] = self.task_meta_info(client.name)

    def _before_service(self, fl_ctx: FLContext):
        # before the service processing
        fl_ctx.remove_prop(FLContextKey.COMMUNICATION_ERROR)
        fl_ctx.remove_prop(FLContextKey.UNAUTHENTICATED)

    def _generate_reply(self, headers, payload, fl_ctx: FLContext):
        # process after the service processing
        unauthenticated = fl_ctx.get_prop(FLContextKey.UNAUTHENTICATED)
        if unauthenticated:
            return make_cellnet_reply(rc=F3ReturnCode.UNAUTHENTICATED, error=unauthenticated)

        error = fl_ctx.get_prop(FLContextKey.COMMUNICATION_ERROR)
        if error:
            return make_cellnet_reply(rc=F3ReturnCode.COMM_ERROR, error=error)
        else:
            return_message = new_cell_message(headers, payload)
            return_message.set_header(MessageHeaderKey.RETURN_CODE, F3ReturnCode.OK)
            return return_message

    def register_client(self, request: Message) -> Message:

        """Register new clients on the fly.

        Each client must get registered before getting the global model.
        The server will expect updates from the registered clients
        for multiple federated rounds.

        This function does not change min_num_clients and max_num_clients.
        """

        with self.engine.new_context() as fl_ctx:
            self._before_service(fl_ctx)

            state_check = self.server_state.register(fl_ctx)

            self._handle_state_check(state_check, fl_ctx)
            # if state_check.get(ACTION) in [NIS, ABORT_RUN]:
            #     return make_cellnet_reply(rc=F3ReturnCode.COMM_ERROR, error=state_check.get(MESSAGE))
            # else:
            client = self.client_manager.authenticate(request, fl_ctx)
            if client and client.token:
                self.tokens[client.token] = self.task_meta_info(client.name)
                if self.admin_server:
                    self.admin_server.client_heartbeat(client.token, client.name)

                headers = {
                    CellMessageHeaderKeys.TOKEN: client.token,
                    CellMessageHeaderKeys.SSID: self.server_state.ssid,
                }
            else:
                headers = {}
            return self._generate_reply(headers=headers, payload=None, fl_ctx=fl_ctx)

    def _handle_state_check(self, state_check, fl_ctx: FLContext):
        if state_check.get(ACTION) in [NIS, ABORT_RUN]:
            fl_ctx.set_prop(FLContextKey.COMMUNICATION_ERROR, state_check.get(MESSAGE))

    def _ssid_check(self, client_state, context):
        if client_state.ssid != self.server_state.ssid:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid Service session ID")

    # def Quit(self, request, context):
    # @request_processing
    def quit_client(self, request: Message) -> Message:
        """Existing client quits the federated training process.

        Server will stop sharing the global model with the client,
        further contribution will be rejected.

        This function does not change min_num_clients and max_num_clients.
        """
        # fire_event(EventType.CLIENT_QUIT, self.handlers, self.fl_ctx)

        with self.engine.new_context() as fl_ctx:
            client = self.client_manager.validate_client(request, fl_ctx)
            if client:
                token = client.get_token()
                self.logout_client(token)

            headers = {CellMessageHeaderKeys.MESSAGE: "Removed client"}
            return self._generate_reply(headers=headers, payload=None, fl_ctx=fl_ctx)

    def client_heartbeat(self, request: Message) -> Message:

        with self.engine.new_context() as fl_ctx:
            self._before_service(fl_ctx)

            state_check = self.server_state.heartbeat(fl_ctx)
            self._handle_state_check(state_check, fl_ctx)

            token = request.get_header(CellMessageHeaderKeys.TOKEN)
            client_name = request.get_header(CellMessageHeaderKeys.CLIENT_NAME)

            if self.client_manager.heartbeat(token, client_name, fl_ctx):
                self.tokens[token] = self.task_meta_info(client_name)
            if self.admin_server:
                self.admin_server.client_heartbeat(token, client_name)

            abort_runs = self._sync_client_jobs(request, token)
            reply = self._generate_reply(
                headers={CellMessageHeaderKeys.MESSAGE: "Heartbeat response"}, payload=None, fl_ctx=fl_ctx
            )
            if abort_runs:
                reply.set_header(CellMessageHeaderKeys.ABORT_JOBS, abort_runs)

                display_runs = ",".join(abort_runs)
                self.logger.info(
                    f"These jobs: {display_runs} are not running on the server. "
                    f"Ask client: {client_name} to abort these runs."
                )
            return reply

    def _sync_client_jobs(self, request, client_token):
        # jobs that are running on client but not on server need to be aborted!
        client_jobs = request.get_header(CellMessageHeaderKeys.JOB_IDS)
        server_jobs = self.engine.run_processes.keys()
        jobs_need_abort = list(set(client_jobs).difference(server_jobs))

        # also check jobs that are running on server but not on the client
        jobs_on_server_but_not_on_client = list(set(server_jobs).difference(client_jobs))
        if jobs_on_server_but_not_on_client:
            # should this job be running on the client?
            for job_id in jobs_on_server_but_not_on_client:
                job_info = self.engine.run_processes[job_id]
                participating_clients = job_info.get(RunProcessKey.PARTICIPANTS, None)
                if participating_clients:
                    # this is a dict: token => nvflare.apis.client.Client
                    client = participating_clients.get(client_token, None)
                    if client:
                        self._notify_dead_job(client, job_id)

        return jobs_need_abort

    def _notify_dead_job(self, client, job_id: str):
        try:
            with self.engine.lock:
                shareable = Shareable()
                shareable.set_header(ServerCommandKey.FL_CLIENT, client.name)
                fqcn = FQCN.join([FQCN.ROOT_SERVER, job_id])
                request = new_cell_message({}, fobs.dumps(shareable))
                return_data = self.cell.fire_and_forget(
                    targets=fqcn,
                    channel=CellChannel.SERVER_COMMAND,
                    topic=ServerCommandNames.HANDLE_DEAD_JOB,
                    message=request,
                )
        except BaseException:
            self.logger.info("Could not connect to server runner process")

    def notify_dead_client(self, client):
        """Called to do further processing of the dead client

        Args:
            client: the dead client

        Returns:

        """
        # find all RUNs that this client is participating
        if not self.engine.run_processes:
            return

        for job_id, process_info in self.engine.run_processes.items():
            assert isinstance(process_info, dict)
            participating_clients = process_info.get(RunProcessKey.PARTICIPANTS, None)
            if participating_clients and client.token in participating_clients:
                self._notify_dead_job(client, job_id)

    def start_run(self, job_id, run_root, conf, args, snapshot):
        # Create the FL Engine
        workspace = Workspace(args.workspace, "server", args.config_folder)
        self.run_manager = self.create_run_manager(workspace, job_id)
        self.engine.set_run_manager(self.run_manager)
        self.engine.set_configurator(conf)
        self.engine.asked_to_stop = False

        fed_event_runner = ServerFedEventRunner()
        self.run_manager.add_handler(fed_event_runner)

        try:
            self.server_runner = ServerRunner(config=self.runner_config, job_id=job_id, engine=self.engine)
            self.run_manager.add_handler(self.server_runner)
            self.run_manager.add_component("_Server_Runner", self.server_runner)

            with self.engine.new_context() as fl_ctx:

                if snapshot:
                    self.engine.restore_components(snapshot=snapshot, fl_ctx=FLContext())

                fl_ctx.set_prop(FLContextKey.APP_ROOT, run_root, sticky=True)
                fl_ctx.set_prop(FLContextKey.CURRENT_RUN, job_id, private=False, sticky=True)
                fl_ctx.set_prop(FLContextKey.WORKSPACE_ROOT, args.workspace, private=True, sticky=True)
                fl_ctx.set_prop(FLContextKey.ARGS, args, private=True, sticky=True)
                fl_ctx.set_prop(FLContextKey.WORKSPACE_OBJECT, workspace, private=True)
                fl_ctx.set_prop(FLContextKey.SECURE_MODE, self.secure_train, private=True, sticky=True)
                fl_ctx.set_prop(FLContextKey.RUNNER, self.server_runner, private=True, sticky=True)

            engine_thread = threading.Thread(target=self.run_engine)
            engine_thread.start()

            self.engine.engine_info.status = MachineStatus.STARTED
            while self.engine.engine_info.status != MachineStatus.STOPPED:
                if self.engine.asked_to_stop:
                    self.engine.engine_info.status = MachineStatus.STOPPED

                time.sleep(3)

            if engine_thread.is_alive():
                engine_thread.join()

        finally:
            self.engine.engine_info.status = MachineStatus.STOPPED
            self.run_manager = None

    def create_run_manager(self, workspace, job_id):
        return RunManager(
            server_name=self.project_name,
            engine=self.engine,
            job_id=job_id,
            workspace=workspace,
            components=self.runner_config.components,
            client_manager=self.client_manager,
            handlers=self.runner_config.handlers,
        )

    def abort_run(self):
        with self.engine.new_context() as fl_ctx:
            if self.server_runner:
                self.server_runner.abort(fl_ctx)

    def run_engine(self):
        self.engine.engine_info.status = MachineStatus.STARTED
        self.server_runner.run()
        self.engine.engine_info.status = MachineStatus.STOPPED

    def deploy(self, args, grpc_args=None, secure_train=False):
        super().deploy(args, grpc_args, secure_train)

        target = grpc_args["service"].get("target", "0.0.0.0:6007")
        self.server_state.host = target.split(":")[0]
        self.server_state.service_port = target.split(":")[1]

        self.overseer_agent = self._init_agent(args)

        if secure_train:
            if self.overseer_agent:
                self.overseer_agent.set_secure_context(
                    ca_path=grpc_args["ssl_root_cert"],
                    cert_path=grpc_args["ssl_cert"],
                    prv_key_path=grpc_args["ssl_private_key"],
                )

        self._register_cellnet_cbs()

        self.overseer_agent.start(self.overseer_callback)

    def _init_agent(self, args=None):
        kv_list = parse_vars(args.set)
        sp = kv_list.get("sp")

        if sp:
            with self.engine.new_context() as fl_ctx:
                fl_ctx.set_prop(FLContextKey.SP_END_POINT, sp)
                self.overseer_agent.initialize(fl_ctx)

        return self.overseer_agent

    def overseer_callback(self, overseer_agent):
        if overseer_agent.is_shutdown():
            self.engine.shutdown_server()
            return

        sp = overseer_agent.get_primary_sp()
        # print(sp)
        with self.engine.new_context() as fl_ctx:
            self.server_state = self.server_state.handle_sd_callback(sp, fl_ctx)

        if isinstance(self.server_state, Cold2HotState):
            server_thread = threading.Thread(target=self._turn_to_hot)
            server_thread.start()

        if isinstance(self.server_state, Hot2ColdState):
            server_thread = threading.Thread(target=self._turn_to_cold)
            server_thread.start()

    def _turn_to_hot(self):
        # Restore Snapshot
        with self.snapshot_lock:
            fl_snapshot = self.snapshot_persistor.retrieve()
            if fl_snapshot:
                for run_number, snapshot in fl_snapshot.run_snapshots.items():
                    if snapshot and not snapshot.completed:
                        # Restore the workspace
                        workspace_data = snapshot.get_component_snapshot(SnapshotKey.WORKSPACE).get("content")
                        dst = os.path.join(self.workspace, WorkspaceConstants.WORKSPACE_PREFIX + str(run_number))
                        if os.path.exists(dst):
                            shutil.rmtree(dst, ignore_errors=True)

                        os.makedirs(dst, exist_ok=True)
                        unzip_all_from_bytes(workspace_data, dst)

                        job_id = snapshot.get_component_snapshot(SnapshotKey.JOB_INFO).get(SnapshotKey.JOB_ID)
                        job_clients = snapshot.get_component_snapshot(SnapshotKey.JOB_INFO).get(SnapshotKey.JOB_CLIENTS)
                        self.logger.info(f"Restore the previous snapshot. Run_number: {run_number}")
                        with self.engine.new_context() as fl_ctx:
                            job_runner = self.engine.job_runner
                            job_runner.restore_running_job(
                                run_number=run_number,
                                job_id=job_id,
                                job_clients=job_clients,
                                snapshot=snapshot,
                                fl_ctx=fl_ctx,
                            )

            self.server_state = HotState(
                host=self.server_state.host, port=self.server_state.service_port, ssid=self.server_state.ssid
            )

    def _turn_to_cold(self):
        # Wrap-up server operations
        self.server_state = ColdState(host=self.server_state.host, port=self.server_state.service_port)

    def stop_training(self):
        self.status = ServerStatus.STOPPED
        self.logger.info("Server app stopped.\n\n")

    def fl_shutdown(self):
        self.engine.stop_all_jobs()
        self.engine.fire_event(EventType.SYSTEM_END, self.engine.new_context())

        super().fl_shutdown()

    def close(self):
        """Shutdown the server."""
        self.logger.info("shutting down server")
        self.shutdown = True
        if self.overseer_agent:
            self.overseer_agent.end()
        return super().close()
