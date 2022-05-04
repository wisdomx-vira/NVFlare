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
import io
import json
import logging
from typing import Dict, List

from nvflare.apis.job_def import Job, JobMetaKey
from nvflare.apis.job_def_manager_spec import JobDefManagerSpec
from nvflare.fuel.hci.conn import Connection
from nvflare.fuel.hci.reg import CommandModuleSpec, CommandSpec
from nvflare.fuel.hci.table import Table
from nvflare.fuel.utils.argument_utils import SafeArgumentParser
from nvflare.private.fed.server.server_engine import ServerEngine
from nvflare.security.security import Action

from .training_cmds import TrainingCommandModule


class JobCommandModule(TrainingCommandModule):
    """Command module with commands for job management."""

    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)

    def get_spec(self):
        return CommandModuleSpec(
            name="job_mgmt",
            cmd_specs=[
                CommandSpec(
                    name="list_jobs",
                    description="list submitted jobs",
                    usage="list_jobs [-n name_prefix] [-s study_prefix] [-d] [job_id_prefix]",
                    handler_func=self.list_jobs,
                ),
                CommandSpec(
                    name="delete_job",
                    description="delete a job",
                    usage="delete_job job_id",
                    handler_func=self.delete_job,
                    authz_func=self.authorize_job,
                ),
                CommandSpec(
                    name="abort_job",
                    description="abort a job if it is running or dispatched",
                    usage="abort_job job_id",
                    handler_func=self.abort_job,  # see if running, if running, send abort command
                    authz_func=self.authorize_job,
                ),
                CommandSpec(
                    name="clone_job",
                    description="clone a job with a new job_id",
                    usage="clone_job job_id",
                    handler_func=self.clone_job,
                    authz_func=self.authorize_job,
                ),
            ],
        )

    def authorize_job(self, conn: Connection, args: List[str]):
        if len(args) != 2:
            conn.append_error("syntax error: missing job_id")
            return False, None

        job_id = args[1].lower()
        conn.set_prop(self.RUN_NUMBER, job_id)
        args.append("server")  # for now, checking permissions against server

        return self._authorize_actions(conn, args[2:], [Action.TRAIN])

    def list_jobs(self, conn: Connection, args: List[str]):

        try:
            parser = SafeArgumentParser(prog="list_jobs")
            parser.add_argument("job_id", nargs="?", help="Job ID prefix")
            parser.add_argument("-d", action="store_true", help="Show detailed list")
            parser.add_argument("-n", help="Filter by job name prefix")
            parser.add_argument("-s", help="Filter by study name prefix")
            parsed_args = parser.parse_args(args[1:])

            engine = conn.app_ctx
            if not isinstance(engine, ServerEngine):
                raise TypeError(f"engine is not of type ServerEngine, but got {type(engine)}")
            job_def_manager = engine.job_def_manager
            if not isinstance(job_def_manager, JobDefManagerSpec):
                raise TypeError(
                    f"job_def_manager in engine is not of type JobDefManagerSpec, but got {type(job_def_manager)}"
                )

            with engine.new_context() as fl_ctx:
                jobs = job_def_manager.get_all_jobs(fl_ctx)
            if jobs:
                id_prefix = parsed_args.job_id
                name_prefix = parsed_args.n
                study_prefix = parsed_args.s

                filtered_jobs = [job for job in jobs if self._job_match(job.meta, id_prefix, name_prefix, study_prefix)]
                if not filtered_jobs:
                    conn.append_error("No jobs matching the searching criteria")
                    return

                if len(filtered_jobs) == 1 or parsed_args.d:
                    self._send_detail_list(conn, filtered_jobs)
                else:
                    self._send_summary_list(conn, filtered_jobs)
            else:
                conn.append_string("No jobs.")
        except Exception as e:
            conn.append_error(str(e))
            return

        conn.append_success("")

    def delete_job(self, conn: Connection, args: List[str]):
        job_id = conn.get_prop(self.RUN_NUMBER)
        engine = conn.app_ctx
        try:
            if not isinstance(engine, ServerEngine):
                raise TypeError(f"engine is not of type ServerEngine, but got {type(engine)}")
            job_def_manager = engine.job_def_manager
            if not isinstance(job_def_manager, JobDefManagerSpec):
                raise TypeError(
                    f"job_def_manager in engine is not of type JobDefManagerSpec, but got {type(job_def_manager)}"
                )
            with engine.new_context() as fl_ctx:
                job_def_manager.delete(job_id, fl_ctx)
            conn.append_string("Job {} deleted.".format(job_id))
        except Exception as e:
            conn.append_error("exception occurred: " + str(e))
            return
        conn.append_success("")

    def abort_job(self, conn: Connection, args: List[str]):
        engine = conn.app_ctx
        if not isinstance(engine, ServerEngine):
            raise TypeError("engine must be ServerEngine but got {}".format(type(engine)))

        run_number = conn.get_prop(self.RUN_NUMBER)
        engine.job_runner.stop_run(run_number, engine.new_context())
        conn.append_string("Abort signal has been sent to the server app.")
        conn.append_success("")

    def clone_job(self, conn: Connection, args: List[str]):
        job_id = conn.get_prop(self.RUN_NUMBER)
        engine = conn.app_ctx
        try:
            if not isinstance(engine, ServerEngine):
                raise TypeError(f"engine is not of type ServerEngine, but got {type(engine)}")
            job_def_manager = engine.job_def_manager
            if not isinstance(job_def_manager, JobDefManagerSpec):
                raise TypeError(
                    f"job_def_manager in engine is not of type JobDefManagerSpec, but got {type(job_def_manager)}"
                )
            with engine.new_context() as fl_ctx:
                job = job_def_manager.get_job(job_id, fl_ctx)
                data_bytes = job_def_manager.get_content(job_id, fl_ctx)
                meta = job_def_manager.create(job.meta, data_bytes, fl_ctx)
                conn.append_string("Cloned job {} as: {}".format(job_id, meta.get(JobMetaKey.JOB_ID)))
        except Exception as e:
            conn.append_error("Exception occurred trying to clone job: " + str(e))
            return
        conn.append_success("")

    def _job_match(self, job_meta: Dict, id_prefix: str, name_prefix: str, study_prefix: str) -> bool:
        return (
            ((not id_prefix) or job_meta.get("job_id").lower().startswith(id_prefix.lower()))
            and ((not name_prefix) or job_meta.get("name").lower().startswith(name_prefix.lower()))
            and ((not study_prefix) or job_meta.get("study_name").lower().startswith(study_prefix.lower()))
        )

    def _send_detail_list(self, conn: Connection, jobs: List[Job]):
        for job in jobs:
            conn.append_string(json.dumps(job.meta, indent=4))

    def _send_summary_list(self, conn: Connection, jobs: List[Job]):

        table = Table(["Job ID", "Name", "Study", "Status", "Submit Time"])
        for job in jobs:
            table.add_row(
                [
                    job.meta.get("job_id", ""),
                    job.meta.get("name", ""),
                    job.meta.get("study_name", ""),
                    job.meta.get("status", ""),
                    job.meta.get("submit_time_iso", ""),
                ]
            )

        writer = io.StringIO()
        table.write(writer)
        conn.append_string(writer.getvalue())