# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

import datetime
import json
import logging
import os
import pendulum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, NamedTuple

import prefect
from prefect.utilities.exceptions import AuthorizationError, ClientError
from prefect.utilities.graphql import (
    EnumValue,
    GraphQLResult,
    as_nested_dict,
    parse_graphql,
    with_args,
)

if TYPE_CHECKING:
    import requests
    from prefect.core import Flow
BuiltIn = Union[bool, dict, list, str, set, tuple]

# type definitions for GraphQL results

TaskRunInfoResult = NamedTuple(
    "TaskRunInfoResult",
    [
        ("id", str),
        ("task_id", str),
        ("version", int),
        ("state", "prefect.engine.state.State"),
    ],
)

FlowRunInfoResult = NamedTuple(
    "FlowRunInfoResult",
    [
        ("parameters", Dict[str, Any]),
        ("version", int),
        ("scheduled_start_time", datetime.datetime),
        ("state", "prefect.engine.state.State"),
        ("task_runs", List[TaskRunInfoResult]),
    ],
)


class Client:
    """
    Client for communication with Prefect Cloud

    If the arguments aren't specified the client initialization first checks the prefect
    configuration and if the server is not set there it checks the current context. The
    token will only be present in the current context.

    Args:
        - graphql_server (str, optional): the URL to send all GraphQL requests
            to; if not provided, will be pulled from `cloud.graphql` config var
    """

    def _initialize_logger(self) -> None:
        # The Client requires its own logging setup because the RemoteLogger actually
        # uses a Client to ship its logs; we currently don't send Client logs to Cloud.
        self.logger = logging.getLogger("Client")
        handler = logging.StreamHandler()
        formatter = logging.Formatter(prefect.config.logging.format)
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(prefect.config.logging.level)

    def __init__(self, graphql_server: str = None):
        self._initialize_logger()

        if not graphql_server:
            graphql_server = prefect.config.cloud.get("graphql")
        self.graphql_server = graphql_server

        token = prefect.config.cloud.get("auth_token", None)

        if token is None:
            token_path = os.path.expanduser("~/.prefect/.credentials/auth_token")
            if os.path.exists(token_path):
                with open(token_path, "r") as f:
                    token = f.read() or None
            if token is not None:
                # this is a rare event and we don't expect it to happen
                # leaving this log in case it ever happens we'll know
                self.logger.debug("Client token set from file {}".format(token_path))

        self.token = token

    # -------------------------------------------------------------------------
    # Utilities

    def get(self, path: str, server: str = None, **params: BuiltIn) -> dict:
        """
        Convenience function for calling the Prefect API with token auth and GET request

        Args:
            - path (str): the path of the API url. For example, to GET
                http://prefect-server/v1/auth/login, path would be 'auth/login'.
            - server (str, optional): the server to send the GET request to;
                defaults to `self.graphql_server`
            - params (dict): GET parameters

        Returns:
            - dict: Dictionary representation of the request made
        """
        response = self._request(method="GET", path=path, params=params, server=server)
        if response.text:
            return response.json()
        else:
            return {}

    def post(self, path: str, server: str = None, **params: BuiltIn) -> dict:
        """
        Convenience function for calling the Prefect API with token auth and POST request

        Args:
            - path (str): the path of the API url. For example, to POST
                http://prefect-server/v1/auth/login, path would be 'auth/login'.
            - server (str, optional): the server to send the POST request to;
                defaults to `self.graphql_server`
            - params (dict): POST parameters

        Returns:
            - dict: Dictionary representation of the request made
        """
        response = self._request(method="POST", path=path, params=params, server=server)
        if response.text:
            return response.json()
        else:
            return {}

    def graphql(
        self, query: Any, **variables: Union[bool, dict, str, int]
    ) -> GraphQLResult:
        """
        Convenience function for running queries against the Prefect GraphQL API

        Args:
            - query (Any): A representation of a graphql query to be executed. It will be
                parsed by prefect.utilities.graphql.parse_graphql().
            - **variables (kwarg): Variables to be filled into a query with the key being
                equivalent to the variables that are accepted by the query

        Returns:
            - dict: Data returned from the GraphQL query

        Raises:
            - ClientError if there are errors raised by the GraphQL mutation
        """
        result = self.post(
            path="",
            query=parse_graphql(query),
            variables=json.dumps(variables),
            server=self.graphql_server,
        )

        if "errors" in result:
            raise ClientError(result["errors"])
        else:
            return as_nested_dict(result, GraphQLResult).data  # type: ignore

    def _request(
        self, method: str, path: str, params: dict = None, server: str = None
    ) -> "requests.models.Response":
        """
        Runs any specified request (GET, POST, DELETE) against the server

        Args:
            - method (str): The type of request to be made (GET, POST, DELETE)
            - path (str): Path of the API URL
            - params (dict, optional): Parameters used for the request
            - server (str, optional): The server to make requests against, base API
                server is used if not specified

        Returns:
            - requests.models.Response: The response returned from the request

        Raises:
            - ClientError: if the client token is not in the context (due to not being logged in)
            - ValueError: if a method is specified outside of the accepted GET, POST, DELETE
            - requests.HTTPError: if a status code is returned that is not `200` or `401`
        """
        # lazy import for performance
        import requests

        if server is None:
            server = self.graphql_server
        assert isinstance(server, str)  # mypy assert

        if self.token is None:
            raise AuthorizationError("Call Client.login() to set the client token.")

        url = os.path.join(server, path.lstrip("/")).rstrip("/")

        params = params or {}

        # write this as a function to allow reuse in next try/except block
        def request_fn() -> "requests.models.Response":
            headers = {"Authorization": "Bearer {}".format(self.token)}
            if method == "GET":
                response = requests.get(url, headers=headers, json=params)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=params)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError("Invalid method: {}".format(method))

            # Check if request returned a successful status
            response.raise_for_status()

            return response

        # If a 401 status code is returned, refresh the login token
        try:
            return request_fn()
        except requests.HTTPError as err:
            if err.response.status_code == 401:
                self.refresh_token()
                return request_fn()
            raise

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------

    def login(
        self,
        email: str,
        password: str,
        account_slug: str = None,
        account_id: str = None,
    ) -> None:
        """
        Login to the server in order to gain access

        Args:
            - email (str): User's email on the platform
            - password (str): User's password on the platform
            - account_slug (str, optional): Slug that is unique to the user
            - account_id (str, optional): Specific Account ID for this user to use

        Raises:
            - AuthorizationError if unable to login to the server (request does not return `200`)
        """

        # lazy import for performance
        import requests

        # TODO: This needs to call the main graphql server and be adjusted for auth0
        url = os.path.join(self.graphql_server, "login_email")  # type: ignore
        response = requests.post(
            url,
            auth=(email, password),
            json=dict(account_id=account_id, account_slug=account_slug),
        )

        # Load the current auth token if able to login
        if not response.ok:
            raise AuthorizationError("Could not log in.")
        self.token = response.json().get("token")
        if self.token:
            creds_path = os.path.expanduser("~/.prefect/.credentials")
            if not os.path.exists(creds_path):
                os.makedirs(creds_path)
            with open(os.path.join(creds_path, "auth_token"), "w+") as f:
                f.write(self.token)

    def logout(self) -> None:
        """
        Logs out by clearing all tokens, including deleting `~/.prefect/credentials/auth_token`
        """
        token_path = os.path.expanduser("~/.prefect/.credentials/auth_token")
        if os.path.exists(token_path):
            os.remove(token_path)
        del self.token

    def refresh_token(self) -> None:
        """
        Refresh the auth token for this user on the server. It is only valid for fifteen minutes.
        """
        # lazy import for performance
        import requests

        # TODO: This needs to call the main graphql server
        url = os.path.join(self.graphql_server, "refresh_token")  # type: ignore
        response = requests.post(
            url, headers={"Authorization": "Bearer {}".format(self.token)}
        )
        self.token = response.json().get("token")

    def deploy(
        self, flow: "Flow", project_id: str = None, set_schedule_active: bool = False
    ) -> str:
        """
        Push a new flow to Prefect Cloud

        Args:
            - flow (Flow): a flow to deploy
            - project_id (str, optional): the project that should contain this flow. If `None`, the
                default project will be used ("Flows"). This can be changed later.
            - set_schedule_active (bool, optional): if `True`, will set the
                schedule to active in the database and begin scheduling runs (if the Flow has a schedule).
                Defaults to `False`. This can be changed later.

        Returns:
            - str: the ID of the newly-deployed flow

        Raises:
            - ClientError: if the deploy failed

        """
        required_parameters = flow.parameters(only_required=True)
        if flow.schedule is not None and required_parameters:
            raise ClientError(
                "Flows with required parameters can not be scheduled automatically."
            )

        create_mutation = {
            "mutation($input: createFlowInput!)": {
                "createFlow(input: $input)": {"id", "error"}
            }
        }
        schedule_mutation = {
            "mutation($input: setFlowScheduleStateInput!)": {
                "setFlowScheduleState(input: $input)": {"error"}
            }
        }
        res = self.graphql(
            create_mutation,
            input=dict(projectId=project_id, serializedFlow=flow.serialize(build=True)),
        )  # type: Any

        if res.createFlow.error:
            raise ClientError(res.createFlow.error)

        if set_schedule_active:
            scheduled_res = self.graphql(
                schedule_mutation,
                input=dict(flowId=res.createFlow.id, setActive=True),  # type: ignore
            )  # type: Any
            if scheduled_res.setFlowScheduleState.error:
                raise ClientError(scheduled_res.setFlowScheduleState.error)

        return res.createFlow.id

    def create_flow_run(
        self,
        flow_id: str,
        parameters: dict = None,
        scheduled_start_time: datetime.datetime = None,
    ) -> GraphQLResult:
        """
        Create a new flow run for the given flow id.  If `start_time` is not provided, the flow run will be scheduled to start immediately.

        Args:
            - flow_id (str): the id of the Flow you wish to schedule
            - parameters (dict, optional): a dictionary of parameter values to pass to the flow run
            - scheduled_start_time (datetime, optional): the time to schedule the execution for; if not provided, defaults to now

        Returns:
            - GraphQLResult: a `DotDict` with an `"id"` key representing the id of the newly created flow run

        Raises:
            - ClientError: if the GraphQL query is bad for any reason
        """
        create_mutation = {
            "mutation($input: createFlowRunInput!)": {
                "createFlowRun(input: $input)": {"flow_run": "id"}
            }
        }
        inputs = dict(flowId=flow_id)
        if parameters is not None:
            inputs.update(parameters=parameters)  # type: ignore
        if scheduled_start_time is not None:
            inputs.update(
                scheduledStartTime=scheduled_start_time.isoformat()
            )  # type: ignore
        res = self.graphql(create_mutation, input=inputs)
        return res.createFlowRun.flow_run  # type: ignore

    def get_flow_run_info(self, flow_run_id: str) -> FlowRunInfoResult:
        """
        Retrieves version and current state information for the given flow run.

        Args:
            - flow_run_id (str): the id of the flow run to get information for

        Returns:
            - GraphQLResult: a `DotDict` representing information about the flow run

        Raises:
            - ClientError: if the GraphQL mutation is bad for any reason
        """
        query = {
            "query": {
                with_args("flow_run_by_pk", {"id": flow_run_id}): {
                    "parameters": True,
                    "version": True,
                    "scheduled_start_time": True,
                    "serialized_state": True,
                    # load all task runs except dynamic task runs
                    with_args("task_runs", {"where": {"map_index": {"_eq": -1}}}): {
                        "id",
                        "task_id",
                        "version",
                        "serialized_state",
                    },
                }
            }
        }
        result = self.graphql(query).flow_run_by_pk  # type: ignore
        if result is None:
            raise ClientError('Flow run ID not found: "{}"'.format(flow_run_id))

        # convert scheduled_start_time from string to datetime
        result.scheduled_start_time = pendulum.parse(result.scheduled_start_time)

        # create "state" attribute from serialized_state
        result.state = prefect.engine.state.State.deserialize(
            result.pop("serialized_state")
        )

        # reformat task_runs
        task_runs = []
        for tr in result.task_runs:
            tr.state = prefect.engine.state.State.deserialize(
                tr.pop("serialized_state")
            )
            task_runs.append(TaskRunInfoResult(**tr))

        result.task_runs = task_runs
        return FlowRunInfoResult(**result)

    def update_flow_run_heartbeat(self, flow_run_id: str) -> None:
        """
        Convenience method for heartbeating a flow run.

        Does NOT raise an error if the update fails.

        Args:
            - flow_run_id (str): the flow run ID to heartbeat

        """
        mutation = {
            "mutation": {
                with_args(
                    "updateFlowRunHeartbeat", {"input": {"flowRunId": flow_run_id}}
                ): {"error"}
            }
        }
        self.graphql(mutation)

    def update_task_run_heartbeat(self, task_run_id: str) -> None:
        """
        Convenience method for heartbeating a task run.

        Does NOT raise an error if the update fails.

        Args:
            - task_run_id (str): the task run ID to heartbeat

        """
        mutation = {
            "mutation": {
                with_args(
                    "updateTaskRunHeartbeat", {"input": {"taskRunId": task_run_id}}
                ): {"error"}
            }
        }
        self.graphql(mutation)

    def set_flow_run_state(
        self, flow_run_id: str, version: int, state: "prefect.engine.state.State"
    ) -> None:
        """
        Sets new state for a flow run in the database.

        Args:
            - flow_run_id (str): the id of the flow run to set state for
            - version (int): the current version of the flow run state
            - state (State): the new state for this flow run

        Raises:
            - ClientError: if the GraphQL mutation is bad for any reason
        """
        mutation = {
            "mutation($state: JSON!)": {
                with_args(
                    "setFlowRunState",
                    {
                        "input": {
                            "flowRunId": flow_run_id,
                            "version": version,
                            "state": EnumValue("$state"),
                        }
                    },
                ): {"error"}
            }
        }

        serialized_state = state.serialize()

        result = self.graphql(mutation, state=serialized_state)  # type: Any

        if result.setFlowRunState.error:
            raise ClientError(result.setFlowRunState.error)

    def get_task_run_info(
        self, flow_run_id: str, task_id: str, map_index: Optional[int] = None
    ) -> TaskRunInfoResult:
        """
        Retrieves version and current state information for the given task run.

        Args:
            - flow_run_id (str): the id of the flow run that this task run lives in
            - task_id (str): the task id for this task run
            - map_index (int, optional): the mapping index for this task run; if
                `None`, it is assumed this task is _not_ mapped

        Returns:
            - NamedTuple: a tuple containing `id, task_id, version, state`

        Raises:
            - ClientError: if the GraphQL mutation is bad for any reason
        """
        mutation = {
            "mutation": {
                with_args(
                    "getOrCreateTaskRun",
                    {
                        "input": {
                            "flowRunId": flow_run_id,
                            "taskId": task_id,
                            "mapIndex": -1 if map_index is None else map_index,
                        }
                    },
                ): {"task_run": {"id", "version", "serialized_state"}, "error": True}
            }
        }
        result = self.graphql(mutation)  # type: Any

        if result.getOrCreateTaskRun.error:
            raise ClientError(result.getOrCreateTaskRun.error)
        else:
            result = result.getOrCreateTaskRun.task_run

        state = prefect.engine.state.State.deserialize(result.serialized_state)
        return TaskRunInfoResult(
            id=result.id, task_id=task_id, version=result.version, state=state
        )

    def set_task_run_state(
        self,
        task_run_id: str,
        version: int,
        state: "prefect.engine.state.State",
        cache_for: datetime.timedelta = None,
    ) -> None:
        """
        Sets new state for a task run.

        Args:
            - task_run_id (str): the id of the task run to set state for
            - version (int): the current version of the task run state
            - state (State): the new state for this task run
            - cache_for (timedelta, optional): how long to store the result of this task for, using the
                serializer set in config; if not provided, no caching occurs

        Raises:
            - ClientError: if the GraphQL mutation is bad for any reason
        """
        mutation = {
            "mutation($state: JSON!)": {
                with_args(
                    "setTaskRunState",
                    {
                        "input": {
                            "taskRunId": task_run_id,
                            "version": version,
                            "state": EnumValue("$state"),
                        }
                    },
                ): {"error"}
            }
        }

        serialized_state = state.serialize()

        result = self.graphql(mutation, state=serialized_state)  # type: Any
        if result.setTaskRunState.error:
            raise ClientError(result.setTaskRunState.error)

    def set_secret(self, name: str, value: Any) -> None:
        """
        Set a secret with the given name and value.

        Args:
            - name (str): the name of the secret; used for retrieving the secret
                during task runs
            - value (Any): the value of the secret

        Raises:
            - ClientError: if the GraphQL mutation is bad for any reason
        """
        mutation = {
            "mutation": {
                with_args("setSecret", {"input": dict(name=name, value=value)}): "error"
            }
        }

        result = self.graphql(mutation)  # type: Any

        if result.setSecret.error:
            raise ClientError(result.setSecret.error)