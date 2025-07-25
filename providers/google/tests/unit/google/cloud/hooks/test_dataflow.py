#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import copy
import logging
import re
import shlex
import subprocess
from asyncio import Future
from unittest import mock
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from google.cloud.dataflow_v1beta3 import (
    GetJobMetricsRequest,
    GetJobRequest,
    JobView,
    ListJobMessagesRequest,
    ListJobsRequest,
)
from google.cloud.dataflow_v1beta3.types import JobMessageImportance

from airflow.exceptions import AirflowException
from airflow.providers.apache.beam.hooks.beam import run_beam_command
from airflow.providers.google.cloud.hooks.dataflow import (
    DEFAULT_DATAFLOW_LOCATION,
    AsyncDataflowHook,
    DataflowHook,
    DataflowJobStatus,
    DataflowJobType,
    _DataflowJobsController,
    _fallback_to_project_id_from_variables,
    process_line_and_extract_dataflow_job_id_callback,
)

JOB_NAME = "test-dataflow-pipeline"
MOCK_UUID = UUID("cf4a56d2-8101-4217-b027-2af6216feb48")
MOCK_UUID_PREFIX = str(MOCK_UUID)[:8]
UNIQUE_JOB_NAME = f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}"
TEST_TEMPLATE = "gs://dataflow-templates/wordcount/template_file"
PARAMETERS = {
    "inputFile": "gs://dataflow-samples/shakespeare/kinglear.txt",
    "output": "gs://test/output/my_output",
}
RUNTIME_ENV = {
    "additionalExperiments": ["exp_flag1", "exp_flag2"],
    "additionalUserLabels": {"name": "wrench", "mass": "1.3kg", "count": "3"},
    "bypassTempDirValidation": {},
    "ipConfiguration": "WORKER_IP_PRIVATE",
    "kmsKeyName": (
        "projects/TEST_PROJECT_ID/locations/TEST_LOCATIONS/keyRings/TEST_KEYRING/cryptoKeys/TEST_CRYPTOKEYS"
    ),
    "maxWorkers": 10,
    "network": "default",
    "numWorkers": 2,
    "serviceAccountEmail": "test@apache.airflow",
    "subnetwork": "regions/REGION/subnetworks/SUBNETWORK",
    "tempLocation": "gs://test/temp",
    "workerRegion": "test-region",
    "workerZone": "test-zone",
    "zone": "us-central1-f",
    "machineType": "n1-standard-1",
}
DATAFLOW_STRING = "airflow.providers.google.cloud.hooks.dataflow.{}"
TEST_PROJECT = "test-project"
TEST_JOB_ID = "test-job-id"
TEST_JOBS_FILTER = ListJobsRequest.Filter.ACTIVE
TEST_LOCATION = "custom-location"
TEST_FLEX_PARAMETERS = {
    "containerSpecGcsPath": "gs://test-bucket/test-file",
    "jobName": "test-job-name",
    "parameters": {
        "inputSubscription": "test-subscription",
        "outputTable": "test-project:test-dataset.streaming_beam_sql",
    },
}
TEST_PROJECT_ID = "test-project-id"

TEST_SQL_JOB_NAME = "test-sql-job-name"
TEST_DATASET = "test-dataset"
TEST_SQL_OPTIONS = {
    "bigquery-project": TEST_PROJECT,
    "bigquery-dataset": TEST_DATASET,
    "bigquery-table": "beam_output",
    "bigquery-write-disposition": "write-truncate",
}
TEST_SQL_QUERY = """
SELECT
    sales_region as sales_region,
    count(state_id) as count_state
FROM
    bigquery.table.test-project.beam_samples.beam_table
GROUP BY sales_region;
"""
TEST_SQL_JOB_ID = "test-job-id"

TEST_PIPELINE_PARENT = f"projects/{TEST_PROJECT}/locations/{TEST_LOCATION}"
TEST_PIPELINE_NAME = "test-data-pipeline-name"
TEST_PIPELINE_BODY = {
    "name": f"{TEST_PIPELINE_PARENT}/pipelines/{TEST_PIPELINE_NAME}",
    "type": "PIPELINE_TYPE_BATCH",
    "workload": {
        "dataflowFlexTemplateRequest": {
            "launchParameter": {
                "containerSpecGcsPath": "gs://dataflow-templates-us-central1/latest/Word_Count_metadata",
                "jobName": "test-job",
                "environment": {"tempLocation": "test-temp-location"},
                "parameters": {
                    "inputFile": "gs://dataflow-samples/shakespeare/kinglear.txt",
                    "output": "gs://test/output/my_output",
                },
            },
            "projectId": f"{TEST_PROJECT}",
            "location": f"{TEST_LOCATION}",
        }
    },
}

DEFAULT_CANCEL_TIMEOUT = 5 * 60


class TestFallbackToVariables:
    def test_support_project_id_parameter(self):
        mock_instance = mock.MagicMock()

        class FixtureFallback:
            @_fallback_to_project_id_from_variables
            def test_fn(self, *args, **kwargs):
                mock_instance(*args, **kwargs)

        FixtureFallback().test_fn(project_id="TEST")

        mock_instance.assert_called_once_with(project_id="TEST")

    def test_support_project_id_from_variable_parameter(self):
        mock_instance = mock.MagicMock()

        class FixtureFallback:
            @_fallback_to_project_id_from_variables
            def test_fn(self, *args, **kwargs):
                mock_instance(*args, **kwargs)

        FixtureFallback().test_fn(variables={"project": "TEST"})

        mock_instance.assert_called_once_with(project_id="TEST", variables={})

    def test_raise_exception_on_conflict(self):
        mock_instance = mock.MagicMock()

        class FixtureFallback:
            @_fallback_to_project_id_from_variables
            def test_fn(self, *args, **kwargs):
                mock_instance(*args, **kwargs)

        with pytest.raises(
            AirflowException,
            match="The mutually exclusive parameter `project_id` and `project` key in `variables` parameter "
            "are both present\\. Please remove one\\.",
        ):
            FixtureFallback().test_fn(variables={"project": "TEST"}, project_id="TEST2")

    def test_raise_exception_on_positional_argument(self):
        mock_instance = mock.MagicMock()

        class FixtureFallback:
            @_fallback_to_project_id_from_variables
            def test_fn(self, *args, **kwargs):
                mock_instance(*args, **kwargs)

        with pytest.raises(
            AirflowException, match="You must use keyword arguments in this methods rather than positional"
        ):
            FixtureFallback().test_fn({"project": "TEST"}, "TEST2")


@pytest.mark.db_test
class TestDataflowHook:
    def setup_method(self):
        self.dataflow_hook = DataflowHook(gcp_conn_id="google_cloud_default")
        self.dataflow_hook.beam_hook = MagicMock()

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook._authorize")
    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.build")
    def test_dataflow_client_creation(self, mock_build, mock_authorize):
        result = self.dataflow_hook.get_conn()
        mock_build.assert_called_once_with(
            "dataflow", "v1b3", http=mock_authorize.return_value, cache_discovery=False
        )
        assert mock_build.return_value == result

    @pytest.mark.parametrize(
        "expected_result, job_name, append_job_name",
        [
            (JOB_NAME, JOB_NAME, False),
            ("test-example", "test_example", False),
            (f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}", JOB_NAME, True),
            (f"test-example-{MOCK_UUID_PREFIX}", "test_example", True),
            ("df-job-1", "df-job-1", False),
            ("df-job", "df-job", False),
            ("dfjob", "dfjob", False),
            ("dfjob1", "dfjob1", False),
        ],
    )
    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    def test_valid_dataflow_job_name(self, _, expected_result, job_name, append_job_name):
        assert (
            self.dataflow_hook.build_dataflow_job_name(job_name=job_name, append_job_name=append_job_name)
            == expected_result
        )

    @pytest.mark.parametrize("job_name", ["1dfjob@", "dfjob@", "df^jo"])
    def test_build_dataflow_job_name_with_invalid_value(self, job_name):
        with pytest.raises(ValueError, match=rf"Invalid job_name \({re.escape(job_name)}\);"):
            self.dataflow_hook.build_dataflow_job_name(job_name=job_name, append_job_name=False)

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_get_job(self, mock_conn, mock_dataflowjob):
        method_fetch_job_by_id = mock_dataflowjob.return_value.fetch_job_by_id

        self.dataflow_hook.get_job(job_id=TEST_JOB_ID, project_id=TEST_PROJECT_ID, location=TEST_LOCATION)
        mock_conn.assert_called_once()
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            project_number=TEST_PROJECT_ID,
            location=TEST_LOCATION,
        )
        method_fetch_job_by_id.assert_called_once_with(TEST_JOB_ID)

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_fetch_job_metrics_by_id(self, mock_conn, mock_dataflowjob):
        method_fetch_job_metrics_by_id = mock_dataflowjob.return_value.fetch_job_metrics_by_id

        self.dataflow_hook.fetch_job_metrics_by_id(
            job_id=TEST_JOB_ID, project_id=TEST_PROJECT_ID, location=TEST_LOCATION
        )
        mock_conn.assert_called_once()
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            project_number=TEST_PROJECT_ID,
            location=TEST_LOCATION,
        )
        method_fetch_job_metrics_by_id.assert_called_once_with(TEST_JOB_ID)

    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_fetch_job_metrics_by_id_controller(self, mock_conn):
        method_get_metrics = (
            mock_conn.return_value.projects.return_value.locations.return_value.jobs.return_value.getMetrics
        )
        self.dataflow_hook.fetch_job_metrics_by_id(
            job_id=TEST_JOB_ID, project_id=TEST_PROJECT_ID, location=TEST_LOCATION
        )

        mock_conn.assert_called_once()
        method_get_metrics.return_value.execute.assert_called_once_with(num_retries=0)
        method_get_metrics.assert_called_once_with(
            jobId=TEST_JOB_ID, projectId=TEST_PROJECT_ID, location=TEST_LOCATION
        )

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_fetch_job_messages_by_id(self, mock_conn, mock_dataflowjob):
        method_fetch_job_messages_by_id = mock_dataflowjob.return_value.fetch_job_messages_by_id

        self.dataflow_hook.fetch_job_messages_by_id(
            job_id=TEST_JOB_ID, project_id=TEST_PROJECT_ID, location=TEST_LOCATION
        )
        mock_conn.assert_called_once()
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            project_number=TEST_PROJECT_ID,
            location=TEST_LOCATION,
        )
        method_fetch_job_messages_by_id.assert_called_once_with(TEST_JOB_ID)

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_fetch_job_autoscaling_events_by_id(self, mock_conn, mock_dataflowjob):
        method_fetch_job_autoscaling_events_by_id = (
            mock_dataflowjob.return_value.fetch_job_autoscaling_events_by_id
        )

        self.dataflow_hook.fetch_job_autoscaling_events_by_id(
            job_id=TEST_JOB_ID, project_id=TEST_PROJECT_ID, location=TEST_LOCATION
        )
        mock_conn.assert_called_once()
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            project_number=TEST_PROJECT_ID,
            location=TEST_LOCATION,
        )
        method_fetch_job_autoscaling_events_by_id.assert_called_once_with(TEST_JOB_ID)

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_wait_for_done(self, mock_conn, mock_dataflowjob):
        method_wait_for_done = mock_dataflowjob.return_value.wait_for_done

        self.dataflow_hook.wait_for_done(
            job_name="JOB_NAME",
            project_id=TEST_PROJECT_ID,
            job_id=TEST_JOB_ID,
            location=TEST_LOCATION,
            multiple_jobs=False,
        )
        mock_conn.assert_called_once()
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            project_number=TEST_PROJECT_ID,
            name="JOB_NAME",
            location=TEST_LOCATION,
            poll_sleep=self.dataflow_hook.poll_sleep,
            job_id=TEST_JOB_ID,
            num_retries=self.dataflow_hook.num_retries,
            multiple_jobs=False,
            drain_pipeline=self.dataflow_hook.drain_pipeline,
            cancel_timeout=self.dataflow_hook.cancel_timeout,
            wait_until_finished=self.dataflow_hook.wait_until_finished,
        )
        method_wait_for_done.assert_called_once_with()


@pytest.mark.db_test
class TestDataflowTemplateHook:
    def setup_method(self):
        self.dataflow_hook = DataflowHook(gcp_conn_id="google_cloud_default")

    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_start_template_dataflow(self, mock_conn, mock_controller, mock_uuid):
        launch_method = (
            mock_conn.return_value.projects.return_value.locations.return_value.templates.return_value.launch
        )
        launch_method.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}
        variables = {"zone": "us-central1-f", "tempLocation": "gs://test/temp"}
        self.dataflow_hook.start_template_dataflow(
            job_name=JOB_NAME,
            variables=copy.deepcopy(variables),
            parameters=PARAMETERS,
            dataflow_template=TEST_TEMPLATE,
            project_id=TEST_PROJECT,
        )

        launch_method.assert_called_once_with(
            body={
                "jobName": f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}",
                "parameters": PARAMETERS,
                "environment": variables,
            },
            gcsPath="gs://dataflow-templates/wordcount/template_file",
            projectId=TEST_PROJECT,
            location=DEFAULT_DATAFLOW_LOCATION,
        )

        mock_controller.assert_called_once_with(
            dataflow=mock_conn.return_value,
            job_id="test-job-id",
            name=f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}",
            num_retries=5,
            poll_sleep=10,
            project_number=TEST_PROJECT,
            location=DEFAULT_DATAFLOW_LOCATION,
            drain_pipeline=False,
            expected_terminal_state=None,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
            wait_until_finished=None,
        )
        mock_controller.return_value.wait_for_done.assert_called_once()

    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_start_template_dataflow_with_custom_region_as_variable(
        self, mock_conn, mock_controller, mock_uuid
    ):
        launch_method = (
            mock_conn.return_value.projects.return_value.locations.return_value.templates.return_value.launch
        )
        launch_method.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}
        self.dataflow_hook.start_template_dataflow(
            job_name=JOB_NAME,
            variables={"region": TEST_LOCATION},
            parameters=PARAMETERS,
            dataflow_template=TEST_TEMPLATE,
            project_id=TEST_PROJECT,
        )

        launch_method.assert_called_once_with(
            projectId=TEST_PROJECT,
            location=TEST_LOCATION,
            gcsPath=TEST_TEMPLATE,
            body=mock.ANY,
        )

        mock_controller.assert_called_once_with(
            dataflow=mock_conn.return_value,
            job_id=TEST_JOB_ID,
            name=UNIQUE_JOB_NAME,
            num_retries=5,
            poll_sleep=10,
            project_number=TEST_PROJECT,
            location=TEST_LOCATION,
            drain_pipeline=False,
            expected_terminal_state=None,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
            wait_until_finished=None,
        )
        mock_controller.return_value.wait_for_done.assert_called_once()

    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_start_template_dataflow_with_custom_region_as_parameter(
        self, mock_conn, mock_controller, mock_uuid
    ):
        launch_method = (
            mock_conn.return_value.projects.return_value.locations.return_value.templates.return_value.launch
        )
        launch_method.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}

        self.dataflow_hook.start_template_dataflow(
            job_name=JOB_NAME,
            variables={},
            parameters=PARAMETERS,
            dataflow_template=TEST_TEMPLATE,
            location=TEST_LOCATION,
            project_id=TEST_PROJECT,
        )

        launch_method.assert_called_once_with(
            body={"jobName": UNIQUE_JOB_NAME, "parameters": PARAMETERS, "environment": {}},
            gcsPath="gs://dataflow-templates/wordcount/template_file",
            projectId=TEST_PROJECT,
            location=TEST_LOCATION,
        )

        mock_controller.assert_called_once_with(
            dataflow=mock_conn.return_value,
            job_id=TEST_JOB_ID,
            name=UNIQUE_JOB_NAME,
            num_retries=5,
            poll_sleep=10,
            project_number=TEST_PROJECT,
            location=TEST_LOCATION,
            drain_pipeline=False,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
            wait_until_finished=None,
            expected_terminal_state=None,
        )
        mock_controller.return_value.wait_for_done.assert_called_once()

    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_start_template_dataflow_with_runtime_env(self, mock_conn, mock_dataflowjob, mock_uuid):
        options_with_runtime_env = copy.deepcopy(RUNTIME_ENV)

        dataflowjob_instance = mock_dataflowjob.return_value
        dataflowjob_instance.wait_for_done.return_value = None

        method = (
            mock_conn.return_value.projects.return_value.locations.return_value.templates.return_value.launch
        )

        method.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}
        self.dataflow_hook.start_template_dataflow(
            job_name=JOB_NAME,
            variables=options_with_runtime_env,
            parameters=PARAMETERS,
            dataflow_template=TEST_TEMPLATE,
            project_id=TEST_PROJECT,
            environment={"numWorkers": 17},
        )
        body = {"jobName": mock.ANY, "parameters": PARAMETERS, "environment": RUNTIME_ENV}
        method.assert_called_once_with(
            projectId=TEST_PROJECT,
            location=DEFAULT_DATAFLOW_LOCATION,
            gcsPath=TEST_TEMPLATE,
            body=body,
        )
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            job_id=TEST_JOB_ID,
            location=DEFAULT_DATAFLOW_LOCATION,
            name=f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}",
            num_retries=5,
            poll_sleep=10,
            project_number=TEST_PROJECT,
            drain_pipeline=False,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
            wait_until_finished=None,
            expected_terminal_state=None,
        )
        mock_uuid.assert_called_once_with()

    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_start_template_dataflow_update_runtime_env(self, mock_conn, mock_dataflowjob, mock_uuid):
        options_with_runtime_env = copy.deepcopy(RUNTIME_ENV)
        del options_with_runtime_env["numWorkers"]
        runtime_env = {"numWorkers": 17}
        expected_runtime_env = copy.deepcopy(RUNTIME_ENV)
        expected_runtime_env.update(runtime_env)

        dataflowjob_instance = mock_dataflowjob.return_value
        dataflowjob_instance.wait_for_done.return_value = None

        method = (
            mock_conn.return_value.projects.return_value.locations.return_value.templates.return_value.launch
        )

        method.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}
        self.dataflow_hook.start_template_dataflow(
            job_name=JOB_NAME,
            variables=options_with_runtime_env,
            parameters=PARAMETERS,
            dataflow_template=TEST_TEMPLATE,
            project_id=TEST_PROJECT,
            environment=runtime_env,
        )
        body = {"jobName": mock.ANY, "parameters": PARAMETERS, "environment": expected_runtime_env}
        method.assert_called_once_with(
            projectId=TEST_PROJECT,
            location=DEFAULT_DATAFLOW_LOCATION,
            gcsPath=TEST_TEMPLATE,
            body=body,
        )
        mock_dataflowjob.assert_called_once_with(
            dataflow=mock_conn.return_value,
            job_id=TEST_JOB_ID,
            location=DEFAULT_DATAFLOW_LOCATION,
            name=f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}",
            num_retries=5,
            poll_sleep=10,
            project_number=TEST_PROJECT,
            drain_pipeline=False,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
            wait_until_finished=None,
            expected_terminal_state=None,
        )
        mock_uuid.assert_called_once_with()

    @mock.patch(DATAFLOW_STRING.format("uuid.uuid4"), return_value=MOCK_UUID)
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_launch_job_with_template(self, mock_conn, mock_uuid):
        launch_method = (
            mock_conn.return_value.projects.return_value.locations.return_value.templates.return_value.launch
        )
        launch_method.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}
        variables = {"zone": "us-central1-f", "tempLocation": "gs://test/temp"}
        result = self.dataflow_hook.launch_job_with_template(
            job_name=JOB_NAME,
            variables=copy.deepcopy(variables),
            parameters=PARAMETERS,
            dataflow_template=TEST_TEMPLATE,
            project_id=TEST_PROJECT,
        )

        launch_method.assert_called_once_with(
            body={
                "jobName": f"test-dataflow-pipeline-{MOCK_UUID_PREFIX}",
                "parameters": PARAMETERS,
                "environment": variables,
            },
            gcsPath="gs://dataflow-templates/wordcount/template_file",
            projectId=TEST_PROJECT,
            location=DEFAULT_DATAFLOW_LOCATION,
        )
        assert result == {"id": TEST_JOB_ID}

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_start_flex_template(self, mock_conn, mock_controller):
        expected_job = {"id": TEST_JOB_ID}

        mock_locations = mock_conn.return_value.projects.return_value.locations
        launch_method = mock_locations.return_value.flexTemplates.return_value.launch
        launch_method.return_value.execute.return_value = {"job": expected_job}
        mock_controller.return_value.get_jobs.return_value = [{"id": TEST_JOB_ID}]

        on_new_job_callback = mock.MagicMock()
        result = self.dataflow_hook.start_flex_template(
            body={"launchParameter": TEST_FLEX_PARAMETERS},
            location=TEST_LOCATION,
            project_id=TEST_PROJECT_ID,
            on_new_job_callback=on_new_job_callback,
        )
        on_new_job_callback.assert_called_once_with(expected_job)
        launch_method.assert_called_once_with(
            projectId="test-project-id",
            body={"launchParameter": TEST_FLEX_PARAMETERS},
            location=TEST_LOCATION,
        )
        mock_controller.assert_called_once_with(
            dataflow=mock_conn.return_value,
            project_number=TEST_PROJECT_ID,
            job_id=TEST_JOB_ID,
            location=TEST_LOCATION,
            poll_sleep=self.dataflow_hook.poll_sleep,
            num_retries=self.dataflow_hook.num_retries,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
            wait_until_finished=self.dataflow_hook.wait_until_finished,
        )
        mock_controller.return_value.get_jobs.assert_called_once_with(refresh=True)
        assert result == {"id": TEST_JOB_ID}

    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_launch_job_with_flex_template(self, mock_conn):
        expected_job = {"id": TEST_JOB_ID}

        mock_locations = mock_conn.return_value.projects.return_value.locations
        launch_method = mock_locations.return_value.flexTemplates.return_value.launch
        launch_method.return_value.execute.return_value = {"job": expected_job}

        result = self.dataflow_hook.launch_job_with_flex_template(
            body={"launchParameter": TEST_FLEX_PARAMETERS},
            location=TEST_LOCATION,
            project_id=TEST_PROJECT_ID,
        )
        launch_method.assert_called_once_with(
            projectId="test-project-id",
            body={"launchParameter": TEST_FLEX_PARAMETERS},
            location=TEST_LOCATION,
        )
        assert result == {"id": TEST_JOB_ID}

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController"))
    @mock.patch(DATAFLOW_STRING.format("DataflowHook.get_conn"))
    def test_cancel_job(self, mock_get_conn, jobs_controller):
        self.dataflow_hook.cancel_job(
            job_name=UNIQUE_JOB_NAME, job_id=TEST_JOB_ID, project_id=TEST_PROJECT, location=TEST_LOCATION
        )
        jobs_controller.assert_called_once_with(
            dataflow=mock_get_conn.return_value,
            job_id=TEST_JOB_ID,
            location=TEST_LOCATION,
            name=UNIQUE_JOB_NAME,
            poll_sleep=10,
            project_number=TEST_PROJECT,
            num_retries=5,
            drain_pipeline=False,
            cancel_timeout=DEFAULT_CANCEL_TIMEOUT,
        )
        jobs_controller.cancel()

    def test_extract_job_id_raises_exception(self):
        with pytest.raises(AirflowException):
            self.dataflow_hook.extract_job_id({"not_id": True})


class TestDataflowJob:
    def setup_method(self):
        self.mock_dataflow = MagicMock()

    def test_dataflow_job_init_with_job_id(self):
        mock_jobs = MagicMock()
        self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value = mock_jobs
        _DataflowJobsController(
            self.mock_dataflow, TEST_PROJECT, TEST_LOCATION, 10, UNIQUE_JOB_NAME, TEST_JOB_ID
        ).get_jobs()
        mock_jobs.get.assert_called_once_with(
            projectId=TEST_PROJECT, location=TEST_LOCATION, jobId=TEST_JOB_ID
        )

    def test_dataflow_job_init_without_job_id(self):
        job = {"id": TEST_JOB_ID, "name": UNIQUE_JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_DONE}

        mock_list = self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list
        (mock_list.return_value.execute.return_value) = {"jobs": [job]}

        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next.return_value
        ) = None

        _DataflowJobsController(
            self.mock_dataflow, TEST_PROJECT, TEST_LOCATION, 10, UNIQUE_JOB_NAME
        ).get_jobs()

        mock_list.assert_called_once_with(projectId=TEST_PROJECT, location=TEST_LOCATION)

    def test_dataflow_job_wait_for_multiple_jobs(self):
        job = {
            "id": TEST_JOB_ID,
            "name": UNIQUE_JOB_NAME,
            "type": DataflowJobType.JOB_TYPE_BATCH,
            "currentState": DataflowJobStatus.JOB_STATE_DONE,
        }

        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.return_value
        ) = {"jobs": [job, job]}
        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next.return_value
        ) = None

        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=10,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=True,
        )
        dataflow_job.wait_for_done()

        self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list.assert_called_once_with(
            location=TEST_LOCATION, projectId=TEST_PROJECT
        )

        self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.assert_called_once_with(
            num_retries=20
        )

        assert dataflow_job.get_jobs() == [job, job]

    @pytest.mark.parametrize(
        "state, exception_regex",
        [
            (DataflowJobStatus.JOB_STATE_FAILED, "unexpected terminal state: JOB_STATE_FAILED"),
            (DataflowJobStatus.JOB_STATE_CANCELLED, "unexpected terminal state: JOB_STATE_CANCELLED"),
            (DataflowJobStatus.JOB_STATE_DRAINED, "unexpected terminal state: JOB_STATE_DRAINED"),
            (DataflowJobStatus.JOB_STATE_UPDATED, "unexpected terminal state: JOB_STATE_UPDATED"),
            (
                DataflowJobStatus.JOB_STATE_UNKNOWN,
                "JOB_STATE_UNKNOWN",
            ),
        ],
    )
    def test_dataflow_job_wait_for_multiple_jobs_and_one_in_terminal_state(self, state, exception_regex):
        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.return_value
        ) = {
            "jobs": [
                {
                    "id": "id-1",
                    "name": "name-1",
                    "type": DataflowJobType.JOB_TYPE_BATCH,
                    "currentState": DataflowJobStatus.JOB_STATE_DONE,
                },
                {
                    "id": "id-2",
                    "name": "name-2",
                    "type": DataflowJobType.JOB_TYPE_BATCH,
                    "currentState": state,
                },
            ]
        }
        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next.return_value
        ) = None

        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
        )
        with pytest.raises(AirflowException, match=exception_regex):
            dataflow_job.wait_for_done()

    def test_dataflow_job_wait_for_multiple_jobs_and_streaming_jobs(self):
        mock_jobs_list = (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list
        )
        mock_jobs_list.return_value.execute.return_value = {
            "jobs": [
                {
                    "id": "id-2",
                    "name": "name-2",
                    "currentState": DataflowJobStatus.JOB_STATE_RUNNING,
                    "type": DataflowJobType.JOB_TYPE_STREAMING,
                }
            ]
        }
        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next.return_value
        ) = None

        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
        )
        dataflow_job.wait_for_done()

        assert mock_jobs_list.call_count == 1

    def test_dataflow_job_wait_for_single_jobs(self):
        job = {
            "id": TEST_JOB_ID,
            "name": UNIQUE_JOB_NAME,
            "type": DataflowJobType.JOB_TYPE_BATCH,
            "currentState": DataflowJobStatus.JOB_STATE_DONE,
        }

        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.get.return_value.execute.return_value
        ) = job

        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next.return_value
        ) = None

        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=10,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=False,
        )
        dataflow_job.wait_for_done()

        self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.get.assert_called_once_with(
            jobId=TEST_JOB_ID, location=TEST_LOCATION, projectId=TEST_PROJECT
        )

        self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.get.return_value.execute.assert_called_once_with(
            num_retries=20
        )

        assert dataflow_job.get_jobs() == [job]

    def test_dataflow_job_is_job_running_with_no_job(self):
        mock_jobs_list = (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list
        )
        mock_jobs_list.return_value.execute.return_value = {"jobs": []}
        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next.return_value
        ) = None

        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
        )
        result = dataflow_job.is_job_running()

        assert result is False

    @pytest.mark.parametrize(
        "job_type, job_state, wait_until_finished, expected_result",
        [
            # RUNNING
            (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_RUNNING, None, False),
            (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_RUNNING, None, True),
            (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_RUNNING, True, False),
            (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_RUNNING, True, False),
            (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_RUNNING, False, True),
            (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_RUNNING, False, True),
            # AWAITING STATE
            (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_PENDING, None, False),
            (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_PENDING, None, False),
            (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_PENDING, True, False),
            (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_PENDING, True, False),
            (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_PENDING, False, True),
            (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_PENDING, False, True),
        ],
    )
    def test_check_dataflow_job_state_wait_until_finished(
        self, job_type, job_state, wait_until_finished, expected_result
    ):
        job = {"id": "id-2", "name": "name-2", "type": job_type, "currentState": job_state}
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
            wait_until_finished=wait_until_finished,
        )
        result = dataflow_job.job_reached_terminal_state(job, wait_until_finished)
        assert result == expected_result

    @pytest.mark.parametrize(
        "jobs, wait_until_finished, expected_result",
        [
            # STREAMING
            (
                [
                    (None, DataflowJobStatus.JOB_STATE_QUEUED),
                    (None, DataflowJobStatus.JOB_STATE_PENDING),
                    (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_RUNNING),
                ],
                None,
                True,
            ),
            (
                [
                    (None, DataflowJobStatus.JOB_STATE_QUEUED),
                    (None, DataflowJobStatus.JOB_STATE_PENDING),
                    (DataflowJobType.JOB_TYPE_STREAMING, DataflowJobStatus.JOB_STATE_RUNNING),
                ],
                True,
                False,
            ),
            # BATCH
            (
                [
                    (None, DataflowJobStatus.JOB_STATE_QUEUED),
                    (None, DataflowJobStatus.JOB_STATE_PENDING),
                    (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_RUNNING),
                ],
                False,
                True,
            ),
            (
                [
                    (None, DataflowJobStatus.JOB_STATE_QUEUED),
                    (None, DataflowJobStatus.JOB_STATE_PENDING),
                    (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_RUNNING),
                ],
                None,
                False,
            ),
            (
                [
                    (None, DataflowJobStatus.JOB_STATE_QUEUED),
                    (None, DataflowJobStatus.JOB_STATE_PENDING),
                    (DataflowJobType.JOB_TYPE_BATCH, DataflowJobStatus.JOB_STATE_DONE),
                ],
                None,
                True,
            ),
        ],
    )
    def test_check_dataflow_job_state_without_job_type_changed_on_terminal_state(
        self, jobs, wait_until_finished, expected_result
    ):
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
            wait_until_finished=wait_until_finished,
        )
        result = False
        for current_job in jobs:
            job = {"id": "id-2", "name": "name-2", "type": current_job[0], "currentState": current_job[1]}
            result = dataflow_job.job_reached_terminal_state(job, wait_until_finished)
        assert result == expected_result

    @pytest.mark.parametrize(
        "job_state, wait_until_finished, expected_result",
        [
            # DONE
            (DataflowJobStatus.JOB_STATE_DONE, None, True),
            (DataflowJobStatus.JOB_STATE_DONE, True, True),
            (DataflowJobStatus.JOB_STATE_DONE, False, True),
            # RUNNING
            (DataflowJobStatus.JOB_STATE_RUNNING, None, False),
            (DataflowJobStatus.JOB_STATE_RUNNING, True, False),
            (DataflowJobStatus.JOB_STATE_RUNNING, False, True),
            # AWAITING STATE
            (DataflowJobStatus.JOB_STATE_PENDING, None, False),
            (DataflowJobStatus.JOB_STATE_PENDING, True, False),
            (DataflowJobStatus.JOB_STATE_PENDING, False, True),
        ],
    )
    def test_check_dataflow_job_state_without_job_type(self, job_state, wait_until_finished, expected_result):
        job = {"id": "id-2", "name": "name-2", "currentState": job_state}
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
            wait_until_finished=wait_until_finished,
        )
        result = dataflow_job.job_reached_terminal_state(job, wait_until_finished)
        assert result == expected_result

    @pytest.mark.parametrize(
        "job_type, job_state, exception_regex",
        [
            (
                DataflowJobType.JOB_TYPE_BATCH,
                DataflowJobStatus.JOB_STATE_FAILED,
                "JOB_STATE_FAILED",
            ),
            (
                DataflowJobType.JOB_TYPE_STREAMING,
                DataflowJobStatus.JOB_STATE_FAILED,
                "JOB_STATE_FAILED",
            ),
            (
                DataflowJobType.JOB_TYPE_STREAMING,
                DataflowJobStatus.JOB_STATE_UNKNOWN,
                "JOB_STATE_UNKNOWN",
            ),
            (
                DataflowJobType.JOB_TYPE_BATCH,
                DataflowJobStatus.JOB_STATE_UNKNOWN,
                "JOB_STATE_UNKNOWN",
            ),
            (
                DataflowJobType.JOB_TYPE_BATCH,
                DataflowJobStatus.JOB_STATE_CANCELLED,
                "JOB_STATE_CANCELLED",
            ),
            (
                DataflowJobType.JOB_TYPE_STREAMING,
                DataflowJobStatus.JOB_STATE_CANCELLED,
                "JOB_STATE_CANCELLED",
            ),
            (
                DataflowJobType.JOB_TYPE_BATCH,
                DataflowJobStatus.JOB_STATE_DRAINED,
                "JOB_STATE_DRAINED",
            ),
            (
                DataflowJobType.JOB_TYPE_STREAMING,
                DataflowJobStatus.JOB_STATE_DRAINED,
                "JOB_STATE_DRAINED",
            ),
            (
                DataflowJobType.JOB_TYPE_BATCH,
                DataflowJobStatus.JOB_STATE_UPDATED,
                "JOB_STATE_UPDATED",
            ),
            (
                DataflowJobType.JOB_TYPE_STREAMING,
                DataflowJobStatus.JOB_STATE_UPDATED,
                "JOB_STATE_UPDATED",
            ),
        ],
    )
    def test_check_dataflow_job_state_terminal_state(self, job_type, job_state, exception_regex):
        job = {"id": "id-2", "name": "name-2", "type": job_type, "currentState": job_state}
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name="name-",
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=None,
            num_retries=20,
            multiple_jobs=True,
        )
        with pytest.raises(AirflowException, match=exception_regex):
            dataflow_job.job_reached_terminal_state(job)

    @pytest.mark.parametrize(
        "job_type, expected_terminal_state, match",
        [
            (
                DataflowJobType.JOB_TYPE_BATCH,
                "test",
                "invalid",
            ),
            (
                DataflowJobType.JOB_TYPE_STREAMING,
                DataflowJobStatus.JOB_STATE_DONE,
                "cannot be JOB_STATE_DONE while it is a streaming job",
            ),
            (
                DataflowJobType.JOB_TYPE_BATCH,
                DataflowJobStatus.JOB_STATE_DRAINED,
                "cannot be JOB_STATE_DRAINED while it is a batch job",
            ),
        ],
    )
    def test_check_dataflow_job_state__invalid_expected_state(self, job_type, expected_terminal_state, match):
        job = {
            "id": "id-2",
            "name": "name-2",
            "type": job_type,
            "currentState": DataflowJobStatus.JOB_STATE_QUEUED,
        }
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=False,
            expected_terminal_state=expected_terminal_state,
        )
        with pytest.raises(AirflowException, match=match):
            dataflow_job.job_reached_terminal_state(job, custom_terminal_state=expected_terminal_state)

    def test_dataflow_job_cancel_job(self):
        mock_jobs = self.mock_dataflow.projects.return_value.locations.return_value.jobs
        get_method = mock_jobs.return_value.get
        get_method.return_value.execute.side_effect = [
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_RUNNING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_PENDING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_QUEUED},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_DRAINING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_STOPPED},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLED},
        ]

        mock_jobs.return_value.list_next.return_value = None
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=False,
        )
        dataflow_job.cancel()

        get_method.assert_called_with(jobId=TEST_JOB_ID, location=TEST_LOCATION, projectId=TEST_PROJECT)
        get_method.return_value.execute.assert_called_with(num_retries=20)

        mock_update = mock_jobs.return_value.update
        mock_update.assert_called_once_with(
            body={"requestedState": "JOB_STATE_CANCELLED"},
            jobId="test-job-id",
            location=TEST_LOCATION,
            projectId="test-project",
        )
        mock_update.return_value.execute.assert_called_once_with(num_retries=20)

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.timeout")
    @mock.patch("time.sleep")
    def test_dataflow_job_cancel_job_cancel_timeout(self, mock_sleep, mock_timeout):
        mock_jobs = self.mock_dataflow.projects.return_value.locations.return_value.jobs
        get_method = mock_jobs.return_value.get
        get_method.return_value.execute.side_effect = [
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLING},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLED},
        ]

        mock_jobs.return_value.list_next.return_value = None
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=4,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=False,
            cancel_timeout=10,
        )
        dataflow_job.cancel()

        get_method.assert_called_with(jobId=TEST_JOB_ID, location=TEST_LOCATION, projectId=TEST_PROJECT)
        get_method.return_value.execute.assert_called_with(num_retries=20)

        mock_update = mock_jobs.return_value.update
        mock_update.assert_called_once_with(
            body={"requestedState": "JOB_STATE_CANCELLED"},
            jobId="test-job-id",
            location=TEST_LOCATION,
            projectId="test-project",
        )
        mock_update.return_value.execute.assert_called_once_with(num_retries=20)

        mock_sleep.assert_has_calls([mock.call(4), mock.call(4), mock.call(4)])
        mock_timeout.assert_called_once_with(
            seconds=10, error_message="Canceling jobs failed due to timeout (10s): test-job-id"
        )

    @pytest.mark.parametrize(
        "drain_pipeline, job_type, requested_state",
        [
            (False, "JOB_TYPE_BATCH", "JOB_STATE_CANCELLED"),
            (False, "JOB_TYPE_STREAMING", "JOB_STATE_CANCELLED"),
            (True, "JOB_TYPE_BATCH", "JOB_STATE_CANCELLED"),
            (True, "JOB_TYPE_STREAMING", "JOB_STATE_DRAINED"),
        ],
    )
    def test_dataflow_job_cancel_or_drain_job(self, drain_pipeline, job_type, requested_state):
        job = {
            "id": TEST_JOB_ID,
            "name": UNIQUE_JOB_NAME,
            "currentState": DataflowJobStatus.JOB_STATE_RUNNING,
            "type": job_type,
        }
        get_method = self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.get
        get_method.return_value.execute.return_value = job

        job_list_nest_method = (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list_next
        )
        job_list_nest_method.return_value = None

        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=10,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=False,
            drain_pipeline=drain_pipeline,
            cancel_timeout=None,
        )
        dataflow_job.cancel()

        get_method.assert_called_once_with(jobId=TEST_JOB_ID, location=TEST_LOCATION, projectId=TEST_PROJECT)

        get_method.return_value.execute.assert_called_once_with(num_retries=20)

        mock_update = self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.update
        mock_update.assert_called_once_with(
            body={"requestedState": requested_state},
            jobId="test-job-id",
            location=TEST_LOCATION,
            projectId="test-project",
        )
        mock_update.return_value.execute.assert_called_once_with(num_retries=20)

    def test_dataflow_job_cancel_job_no_running_jobs(self):
        mock_jobs = self.mock_dataflow.projects.return_value.locations.return_value.jobs
        get_method = mock_jobs.return_value.get
        get_method.return_value.execute.side_effect = [
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_DONE},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_UPDATED},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_DRAINED},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_FAILED},
            {"id": TEST_JOB_ID, "name": JOB_NAME, "currentState": DataflowJobStatus.JOB_STATE_CANCELLED},
        ]

        mock_jobs.return_value.list_next.return_value = None
        dataflow_job = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            name=UNIQUE_JOB_NAME,
            location=TEST_LOCATION,
            poll_sleep=0,
            job_id=TEST_JOB_ID,
            num_retries=20,
            multiple_jobs=False,
        )
        dataflow_job.cancel()

        get_method.assert_called_with(jobId=TEST_JOB_ID, location=TEST_LOCATION, projectId=TEST_PROJECT)
        get_method.return_value.execute.assert_called_with(num_retries=20)

        mock_jobs.return_value.update.assert_not_called()

    def test_fetch_list_job_messages_responses(self):
        mock_list = self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.messages.return_value.list
        mock_list_next = self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.messages.return_value.list_next

        mock_list.return_value.execute.return_value = "response_1"
        mock_list_next.return_value = None

        jobs_controller = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            location=TEST_LOCATION,
            job_id=TEST_JOB_ID,
        )
        result = list(jobs_controller._fetch_list_job_messages_responses(TEST_JOB_ID))

        mock_list.assert_called_once_with(projectId=TEST_PROJECT, location=TEST_LOCATION, jobId=TEST_JOB_ID)
        mock_list_next.assert_called_once_with(
            previous_request=mock_list.return_value, previous_response="response_1"
        )
        assert result == ["response_1"]

    def test_fetch_all_jobs_when_no_jobs_returned(self):
        (
            self.mock_dataflow.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.return_value
        ) = {}

        jobs_controller = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            location=TEST_LOCATION,
            job_id=TEST_JOB_ID,
        )
        result = jobs_controller._fetch_all_jobs()
        assert result == []

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController._fetch_list_job_messages_responses"))
    def test_fetch_job_messages_by_id(self, mock_fetch_responses):
        mock_fetch_responses.return_value = iter(
            [
                {"jobMessages": ["message_1"]},
                {"jobMessages": ["message_2"]},
            ]
        )
        jobs_controller = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            location=TEST_LOCATION,
            job_id=TEST_JOB_ID,
        )
        result = jobs_controller.fetch_job_messages_by_id(TEST_JOB_ID)
        mock_fetch_responses.assert_called_once_with(job_id=TEST_JOB_ID)
        assert result == ["message_1", "message_2"]

    @mock.patch(DATAFLOW_STRING.format("_DataflowJobsController._fetch_list_job_messages_responses"))
    def test_fetch_job_autoscaling_events_by_id(self, mock_fetch_responses):
        mock_fetch_responses.return_value = iter(
            [
                {"autoscalingEvents": ["event_1"]},
                {"autoscalingEvents": ["event_2"]},
            ]
        )
        jobs_controller = _DataflowJobsController(
            dataflow=self.mock_dataflow,
            project_number=TEST_PROJECT,
            location=TEST_LOCATION,
            job_id=TEST_JOB_ID,
        )
        result = jobs_controller.fetch_job_autoscaling_events_by_id(TEST_JOB_ID)
        mock_fetch_responses.assert_called_once_with(job_id=TEST_JOB_ID)
        assert result == ["event_1", "event_2"]


@pytest.mark.db_test
class TestDataflowPipelineHook:
    def setup_method(self):
        self.dataflow_hook = DataflowHook(gcp_conn_id="google_cloud_default")

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook._authorize")
    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.build")
    def test_get_conn(self, mock_build, mock_authorize):
        """
        Test that get_conn is called with the correct params and
        returns the correct API address
        """
        connection = self.dataflow_hook.get_pipelines_conn()
        mock_build.assert_called_once_with(
            "datapipelines", "v1", http=mock_authorize.return_value, cache_discovery=False
        )
        assert mock_build.return_value == connection

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook.build_parent_name")
    def test_build_parent_name(self, mock_build_parent_name):
        """
        Test that build_parent_name is called with the correct params and
        returns the correct parent string
        """
        result = self.dataflow_hook.build_parent_name(
            project_id=TEST_PROJECT,
            location=TEST_LOCATION,
        )
        mock_build_parent_name.assert_called_with(
            project_id=TEST_PROJECT,
            location=TEST_LOCATION,
        )
        assert mock_build_parent_name.return_value == result

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook.get_pipelines_conn")
    def test_create_data_pipeline(self, mock_connection):
        """
        Test that request are called with the correct params
        Test that request returns the correct value
        """
        mock_locations = mock_connection.return_value.projects.return_value.locations
        mock_request = mock_locations.return_value.pipelines.return_value.create
        mock_request.return_value.execute.return_value = TEST_PIPELINE_BODY

        result = self.dataflow_hook.create_data_pipeline(
            body=TEST_PIPELINE_BODY,
            project_id=TEST_PROJECT,
            location=TEST_LOCATION,
        )

        mock_request.assert_called_once_with(
            parent=TEST_PIPELINE_PARENT,
            body=TEST_PIPELINE_BODY,
        )
        assert result == TEST_PIPELINE_BODY

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook.get_pipelines_conn")
    def test_run_data_pipeline(self, mock_connection):
        """
        Test that run_data_pipeline is called with correct parameters and
        calls Google Data Pipelines API
        """
        mock_request = mock_connection.return_value.projects.return_value.locations.return_value.pipelines.return_value.run
        mock_request.return_value.execute.return_value = {"job": {"id": TEST_JOB_ID}}

        result = self.dataflow_hook.run_data_pipeline(
            pipeline_name=TEST_PIPELINE_NAME,
            project_id=TEST_PROJECT,
            location=TEST_LOCATION,
        )

        mock_request.assert_called_once_with(
            name=f"{TEST_PIPELINE_PARENT}/pipelines/{TEST_PIPELINE_NAME}",
            body={},
        )
        assert result == {"job": {"id": TEST_JOB_ID}}

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook.get_pipelines_conn")
    def test_get_data_pipeline(self, mock_connection):
        """
        Test that get_data_pipeline is called with correct parameters and
        calls Google Data Pipelines API
        """
        mock_locations = mock_connection.return_value.projects.return_value.locations
        mock_request = mock_locations.return_value.pipelines.return_value.get
        mock_request.return_value.execute.return_value = TEST_PIPELINE_BODY

        result = self.dataflow_hook.get_data_pipeline(
            pipeline_name=TEST_PIPELINE_NAME,
            project_id=TEST_PROJECT,
            location=TEST_LOCATION,
        )

        mock_request.assert_called_once_with(
            name=f"{TEST_PIPELINE_PARENT}/pipelines/{TEST_PIPELINE_NAME}",
        )
        assert result == TEST_PIPELINE_BODY

    @mock.patch("airflow.providers.google.cloud.hooks.dataflow.DataflowHook.get_pipelines_conn")
    def test_delete_data_pipeline(self, mock_connection):
        """
        Test that delete_data_pipeline is called with correct parameters and
        calls Google Data Pipelines API
        """
        mock_locations = mock_connection.return_value.projects.return_value.locations
        mock_request = mock_locations.return_value.pipelines.return_value.delete
        mock_request.return_value.execute.return_value = None

        result = self.dataflow_hook.delete_data_pipeline(
            pipeline_name=TEST_PIPELINE_NAME,
            project_id=TEST_PROJECT,
            location=TEST_LOCATION,
        )

        mock_request.assert_called_once_with(
            name=f"{TEST_PIPELINE_PARENT}/pipelines/{TEST_PIPELINE_NAME}",
        )
        assert result is None


APACHE_BEAM_V_2_14_0_JAVA_SDK_LOG = f""""\
Dataflow SDK version: 2.14.0
Jun 15, 2020 2:57:28 PM org.apache.beam.runners.dataflow.DataflowRunner run
INFO: To access the Dataflow monitoring console, please navigate to https://console.cloud.google.com/dataflow\
/jobsDetail/locations/europe-west3/jobs/{TEST_JOB_ID}?project=XXX
Submitted job: {TEST_JOB_ID}
Jun 15, 2020 2:57:28 PM org.apache.beam.runners.dataflow.DataflowRunner run
INFO: To cancel the job using the 'gcloud' tool, run:
> gcloud dataflow jobs --project=XXX cancel --region=europe-west3 {TEST_JOB_ID}
"""

APACHE_BEAM_V_2_22_0_JAVA_SDK_LOG = f""""\
INFO: Dataflow SDK version: 2.22.0
Jun 15, 2020 3:09:03 PM org.apache.beam.runners.dataflow.DataflowRunner run
INFO: To access the Dataflow monitoring console, please navigate to https://console.cloud.google.com/dataflow\
/jobs/europe-west3/{TEST_JOB_ID}?project=XXXX
Jun 15, 2020 3:09:03 PM org.apache.beam.runners.dataflow.DataflowRunner run
INFO: Submitted job: {TEST_JOB_ID}
Jun 15, 2020 3:09:03 PM org.apache.beam.runners.dataflow.DataflowRunner run
INFO: To cancel the job using the 'gcloud' tool, run:
> gcloud dataflow jobs --project=XXX cancel --region=europe-west3 {TEST_JOB_ID}
"""

# using logback pattern: [%d] %-5level %msg \(%c\) \(%t\)%n
APACHE_BEAM_V_2_58_1_JAVA_SDK_LOG = f""""\
[2024-08-28 08:06:39,298] INFO  Dataflow SDK version: 2.58.1 (org.apache.beam.runners.dataflow.DataflowRunner) (main)
[2024-08-28 08:06:40,305] INFO  To access the Dataflow monitoring console, please navigate to\
https://console.cloud.google.com/dataflow/jobs/europe-west1/{TEST_JOB_ID}?project=XXXX\
(org.apache.beam.runners.dataflow.DataflowRunner) (main)
[2024-08-28 08:06:40,305] INFO  Submitted job: {TEST_JOB_ID} (org.apache.beam.runners.dataflow.DataflowRunner) (main)
[2024-08-28 08:06:40,305] INFO  To cancel the job using the 'gcloud' tool, run:
> gcloud dataflow jobs --project=gowish-develop cancel --region=europe-west1 {TEST_JOB_ID}\
(org.apache.beam.runners.dataflow.DataflowRunner) (main)
"""

CLOUD_COMPOSER_CLOUD_LOGGING_APACHE_BEAM_V_2_56_0_JAVA_SDK_LOG = f"""\
WARNING - {{"message":"org.apache.beam.runners.dataflow.DataflowRunner - Dataflow SDK version: 2.56.0",\
"severity":"INFO"}}
WARNING - {{"message":"org.apache.beam.runners.dataflow.DataflowRunner - To access the Dataflow monitoring\\
console, please navigate to https://console.cloud.google.com/dataflow/jobs/europe-west3/{TEST_JOB_ID}?project
\u003dXXXX","severity":"INFO"}}
WARNING - {{"message":"org.apache.beam.runners.dataflow.DataflowRunner - Submitted job: {TEST_JOB_ID}",\
"severity":"INFO"}}
WARNING - {{"message":"org.apache.beam.runners.dataflow.DataflowRunner - To cancel the job using the \
\u0027gcloud\u0027 tool, run:\n\u003e gcloud dataflow jobs --project\u003dXXX cancel --region\u003deurope-\
west3 {TEST_JOB_ID}","severity":"INFO"}}
"""

APACHE_BEAM_V_2_14_0_PYTHON_SDK_LOG = f""""\
INFO:root:Completed GCS upload to gs://test-dataflow-example/staging/start-python-job-local-5bcf3d71.\
1592286375.000962/apache_beam-2.14.0-cp37-cp37m-manylinux1_x86_64.whl in 0 seconds.
INFO:root:Create job: <Job
 createTime: '2020-06-16T05:46:20.911857Z'
 currentStateTime: '1970-01-01T00:00:00Z'
 id: '{TEST_JOB_ID}'
 location: 'us-central1'
 name: 'start-python-job-local-5bcf3d71'
 projectId: 'XXX'
 stageStates: []
 startTime: '2020-06-16T05:46:20.911857Z'
 steps: []
 tempFiles: []
 type: TypeValueValuesEnum(JOB_TYPE_BATCH, 1)>
INFO:root:Created job with id: [{TEST_JOB_ID}]
INFO:root:To access the Dataflow monitoring console, please navigate to https://console.cloud.google.com/\
dataflow/jobsDetail/locations/us-central1/jobs/{TEST_JOB_ID}?project=XXX
"""

APACHE_BEAM_V_2_22_0_PYTHON_SDK_LOG = f""""\
INFO:apache_beam.runners.dataflow.internal.apiclient:Completed GCS upload to gs://test-dataflow-example/\
staging/start-python-job-local-5bcf3d71.1592286719.303624/apache_beam-2.22.0-cp37-cp37m-manylinux1_x86_64.whl\
 in 1 seconds.
INFO:apache_beam.runners.dataflow.internal.apiclient:Create job: <Job
 createTime: '2020-06-16T05:52:04.095216Z'
 currentStateTime: '1970-01-01T00:00:00Z'
 id: '{TEST_JOB_ID}'
 location: 'us-central1'
 name: 'start-python-job-local-5bcf3d71'
 projectId: 'XXX'
 stageStates: []
 startTime: '2020-06-16T05:52:04.095216Z'
 steps: []
 tempFiles: []
 type: TypeValueValuesEnum(JOB_TYPE_BATCH, 1)>
INFO:apache_beam.runners.dataflow.internal.apiclient:Created job with id: [{TEST_JOB_ID}]
INFO:apache_beam.runners.dataflow.internal.apiclient:Submitted job: {TEST_JOB_ID}
INFO:apache_beam.runners.dataflow.internal.apiclient:To access the Dataflow monitoring console, please \
navigate to https://console.cloud.google.com/dataflow/jobs/us-central1/{TEST_JOB_ID}?project=XXX
"""


class TestDataflow:
    @pytest.mark.parametrize(
        "log",
        [
            pytest.param(APACHE_BEAM_V_2_14_0_JAVA_SDK_LOG, id="apache-beam-2.14.0-JDK"),
            pytest.param(APACHE_BEAM_V_2_22_0_JAVA_SDK_LOG, id="apache-beam-2.22.0-JDK"),
            pytest.param(APACHE_BEAM_V_2_58_1_JAVA_SDK_LOG, id="apache-beam-2.58.1-JDK"),
            pytest.param(
                CLOUD_COMPOSER_CLOUD_LOGGING_APACHE_BEAM_V_2_56_0_JAVA_SDK_LOG,
                id="cloud-composer-cloud-logging-apache-beam-2.56.0-JDK",
            ),
            pytest.param(APACHE_BEAM_V_2_14_0_PYTHON_SDK_LOG, id="apache-beam-2.14.0-Python"),
            pytest.param(APACHE_BEAM_V_2_22_0_PYTHON_SDK_LOG, id="apache-beam-2.22.0-Python"),
        ],
    )
    def test_data_flow_valid_job_id(self, log):
        echos = ";".join(f"echo {shlex.quote(line)}" for line in log.splitlines())
        cmd = ["bash", "-c", echos]
        found_job_id = None

        def callback(job_id):
            nonlocal found_job_id
            found_job_id = job_id

        mock_log = MagicMock()
        run_beam_command(
            cmd=cmd,
            process_line_callback=process_line_and_extract_dataflow_job_id_callback(callback),
            log=mock_log,
        )
        assert found_job_id == TEST_JOB_ID

    def test_data_flow_missing_job_id(self):
        cmd = ["echo", "unit testing"]
        found_job_id = None

        def callback(job_id):
            nonlocal found_job_id
            found_job_id = job_id

        log = MagicMock()
        run_beam_command(
            cmd=cmd,
            process_line_callback=process_line_and_extract_dataflow_job_id_callback(callback),
            log=log,
        )
        assert found_job_id is None

    @mock.patch("subprocess.Popen")
    @mock.patch("select.select")
    def test_dataflow_wait_for_done_logging(self, mock_select, mock_popen, caplog):
        logger_name = "fake-dataflow-wait-for-done-logger"
        fake_logger = logging.getLogger(logger_name)
        fake_logger.setLevel(logging.INFO)

        cmd = ["fake", "cmd"]
        mock_proc = MagicMock(name="FakeProc")
        fake_stderr_fd = MagicMock(name="FakeStderr")
        fake_stdout_fd = MagicMock(name="FakeStdout")

        mock_proc.stderr = fake_stderr_fd
        mock_proc.stdout = fake_stdout_fd
        fake_stderr_fd.readline.side_effect = [
            b"dataflow-stderr-1",
            b"dataflow-stderr-2",
            StopIteration,
            b"dataflow-stderr-3",
            StopIteration,
            b"dataflow-other-stderr",
        ]
        fake_stdout_fd.readline.side_effect = [b"dataflow-stdout", StopIteration]
        mock_select.side_effect = [
            ([fake_stderr_fd], None, None),
            (None, None, None),
            ([fake_stderr_fd], None, None),
        ]
        mock_proc.poll.side_effect = [None, True]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        caplog.clear()
        caplog.set_level(logging.INFO)
        with pytest.raises(AirflowException, match="Apache Beam process failed with return code 1"):
            run_beam_command(cmd=cmd, log=fake_logger)

        mock_popen.assert_called_once_with(
            cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, cwd=None
        )
        info_messages = [rt[2] for rt in caplog.record_tuples if rt[0] == logger_name and rt[1] == 20]
        assert "Running command: fake cmd" in info_messages
        assert "dataflow-stdout" in info_messages

        warn_messages = [rt[2] for rt in caplog.record_tuples if rt[0] == logger_name and rt[1] == 30]
        assert "dataflow-stderr-1" in warn_messages
        assert "dataflow-stderr-2" in warn_messages
        assert "dataflow-stderr-3" in warn_messages
        assert "dataflow-other-stderr" in warn_messages


@pytest.fixture
def make_mock_awaitable():
    def func(mock_obj, return_value):
        f = Future()
        f.set_result(return_value)
        mock_obj.return_value = f

    return func


class TestAsyncDataflowHook:
    @pytest.fixture
    def hook(self):
        return AsyncDataflowHook(
            gcp_conn_id=TEST_PROJECT_ID,
        )

    @pytest.mark.asyncio
    @mock.patch(DATAFLOW_STRING.format("AsyncDataflowHook.initialize_client"))
    async def test_get_job(self, initialize_client_mock, hook, make_mock_awaitable):
        client = initialize_client_mock.return_value
        make_mock_awaitable(client.get_job, None)

        await hook.get_job(
            project_id=TEST_PROJECT_ID,
            job_id=TEST_JOB_ID,
            location=TEST_LOCATION,
        )
        request = GetJobRequest(
            dict(
                project_id=TEST_PROJECT_ID,
                job_id=TEST_JOB_ID,
                location=TEST_LOCATION,
                view=JobView.JOB_VIEW_SUMMARY,
            )
        )

        initialize_client_mock.assert_called_once()
        client.get_job.assert_called_once_with(
            request=request,
        )

    @pytest.mark.asyncio
    @mock.patch(DATAFLOW_STRING.format("AsyncDataflowHook.initialize_client"))
    async def test_list_jobs(self, initialize_client_mock, hook, make_mock_awaitable):
        client = initialize_client_mock.return_value
        make_mock_awaitable(client.get_job, None)

        await hook.list_jobs(
            project_id=TEST_PROJECT_ID,
            location=TEST_LOCATION,
            jobs_filter=TEST_JOBS_FILTER,
        )

        request = ListJobsRequest(
            {
                "project_id": TEST_PROJECT_ID,
                "location": TEST_LOCATION,
                "filter": TEST_JOBS_FILTER,
                "page_size": None,
                "page_token": None,
            }
        )
        initialize_client_mock.assert_called_once()
        client.list_jobs.assert_called_once_with(request=request)

    @pytest.mark.asyncio
    @mock.patch(DATAFLOW_STRING.format("AsyncDataflowHook.initialize_client"))
    async def test_list_job_messages(self, initialize_client_mock, hook):
        client = initialize_client_mock.return_value
        await hook.list_job_messages(
            project_id=TEST_PROJECT_ID,
            location=TEST_LOCATION,
            job_id=TEST_JOB_ID,
        )
        request = ListJobMessagesRequest(
            {
                "project_id": TEST_PROJECT_ID,
                "job_id": TEST_JOB_ID,
                "minimum_importance": JobMessageImportance.JOB_MESSAGE_BASIC,
                "page_size": None,
                "page_token": None,
                "start_time": None,
                "end_time": None,
                "location": TEST_LOCATION,
            }
        )
        initialize_client_mock.assert_called_once()
        client.list_job_messages.assert_called_once_with(request=request)

    @pytest.mark.asyncio
    @mock.patch(DATAFLOW_STRING.format("AsyncDataflowHook.initialize_client"))
    async def test_get_job_metrics(self, initialize_client_mock, hook):
        client = initialize_client_mock.return_value
        await hook.get_job_metrics(
            project_id=TEST_PROJECT_ID,
            location=TEST_LOCATION,
            job_id=TEST_JOB_ID,
        )
        request = GetJobMetricsRequest(
            {
                "project_id": TEST_PROJECT_ID,
                "job_id": TEST_JOB_ID,
                "start_time": None,
                "location": TEST_LOCATION,
            }
        )
        initialize_client_mock.assert_called_once()
        client.get_job_metrics.assert_called_once_with(request=request)
