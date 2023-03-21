'''
Copyright 2022 The Microsoft DeepSpeed Team
'''
import asyncio
import grpc
import mii
from mii.utils import get_task
from mii.grpc_related.proto import modelresponse_pb2, modelresponse_pb2_grpc
from mii.constants import GRPC_MAX_MSG_SIZE
from mii.method_table import GRPC_METHOD_TABLE


def _get_deployment_info(deployment_name):
    configs = mii.utils.import_score_file(deployment_name).configs
    task = configs[mii.constants.TASK_NAME_KEY]
    mii_configs_dict = configs[mii.constants.MII_CONFIGS_KEY]
    mii_configs = mii.config.MIIConfig(**mii_configs_dict)

    assert task is not None, "The task name should be set before calling init"
    return task, mii_configs


def mii_query_handle(deployment_name):
    """Get a query handle for a local deployment:

        mii/examples/local/gpt2-query-example.py
        mii/examples/local/roberta-qa-query-example.py

    Arguments:
        deployment_name: Name of the deployment. Used as an identifier for posting queries for ``LOCAL`` deployment.

    Returns:
        query_handle: A query handle with a single method `.query(request_dictionary)` using which queries can be sent to the model.
    """
    task_name, mii_configs = _get_deployment_info(deployment_name)
    if mii_configs.enable_load_balancing:
        return MIIClient(task_name, "localhost", mii_configs.port_number)
    else:
        return MIITensorParallelClient(
            task_name,
            "localhost",
            [mii_configs.port_number + i for i in range(mii_configs.tensor_parallel)])


def create_channel(host, port):
    return grpc.aio.insecure_channel(f'{host}:{port}',
                                     options=[('grpc.max_send_message_length',
                                               GRPC_MAX_MSG_SIZE),
                                              ('grpc.max_receive_message_length',
                                               GRPC_MAX_MSG_SIZE)])


class MIIClient():
    """
    Client to send queries to a single endpoint.
    """
    def __init__(self, task_name, host, port):
        self.asyncio_loop = asyncio.get_event_loop()
        channel = create_channel(host, port)
        self.stub = modelresponse_pb2_grpc.ModelResponseStub(channel)
        self.task = get_task(task_name)

    async def _request_async_response(self, request_dict, **query_kwargs):
        if self.task not in GRPC_METHOD_TABLE:
            raise ValueError(f"unknown task: {self.task}")

        conversions = GRPC_METHOD_TABLE[self.task]
        proto_request = conversions["pack_request_to_proto"](request_dict,
                                                             **query_kwargs)
        proto_response = await getattr(self.stub, conversions["method"])(proto_request)
        return conversions["unpack_response_from_proto"](
            proto_response
        ) if "unpack_response_from_proto" in conversions else proto_response

    def query(self, request_dict, **query_kwargs):
        return self.asyncio_loop.run_until_complete(
            self._request_async_response(request_dict,
                                         **query_kwargs))

    async def terminate_async(self):
        await self.stub.Terminate(
            modelresponse_pb2.google_dot_protobuf_dot_empty__pb2.Empty())

    def terminate(self):
        self.asyncio_loop.run_until_complete(self.terminate_async())


class MIITensorParallelClient():
    """
    Client to send queries to multiple endpoints in parallel.
    This is used to call multiple servers deployed for tensor parallelism.
    """
    def __init__(self, task_name, host, ports):
        self.task = get_task(task_name)
        self.clients = [MIIClient(task_name, host, port) for port in ports]
        self.asyncio_loop = asyncio.get_event_loop()
        self._initialize_grpc_client()
        self.tasks = []
        self.results = []
        self.running = False

    def _initialize_grpc_client(self):
        channels = []
        for i in range(self.num_gpus):
            channel = grpc.aio.insecure_channel(f'localhost:{self.port_number + i}',
                                                options=[
                                                    ('grpc.max_send_message_length',
                                                     GRPC_MAX_MSG_SIZE),
                                                    ('grpc.max_receive_message_length',
                                                     GRPC_MAX_MSG_SIZE)
                                                ])
            stub = modelresponse_pb2_grpc.ModelResponseStub(channel)
            channels.append(channel)
            self.stubs.append(stub)

    # runs task in parallel and return the result from the first task
    async def _query_in_tensor_parallel(self, request_string, query_kwargs):
        responses = []
        for client in self.clients:
            responses.append(
                self.asyncio_loop.create_task(
                    client._request_async_response(request_string,
                                                   **query_kwargs)))

        await responses[0]
        return responses[0]

    def query(self, request_dict, **query_kwargs):
        """Query a local deployment:

            mii/examples/local/gpt2-query-example.py
            mii/examples/local/roberta-qa-query-example.py

        Arguments:
            request_dict: A task specific request dictionary consisting of the inputs to the models
            query_kwargs: additional query parameters for the model

        Returns:
            response: Response of the model
        """
        response = self.asyncio_loop.run_until_complete(
            self._query_in_tensor_parallel(request_dict,
                                           query_kwargs))
        ret = response.result()
        return ret

    def query_non_block(self, request_dict, **query_kwargs):
        coro = self._query_in_tensor_parallel(request_dict, query_kwargs)
        self.tasks.append({"id": id(coro), "coro":coro, "run": False})
        return id(coro)
    
    def get_pending_task_result(self, id):
        result = next((item for item in self.results if item["id"] == id), None)
        if result is not None:
            self.results = [item for item in self.results if item['id'] != id]
            return result['result']
        # print(f"\ncalled id = {id}-----")
        if not self.running and len(self.tasks) > 0:
            try:
                self.running = True
                task = self.tasks[0]
                print(f"{task['id']} started, queue={len(self.tasks)}")
                self.tasks.remove(task)
                response = self.asyncio_loop.run_until_complete(task['coro'])
                print(f"{task['id']} complete")
                self.running = False
                if id == task['id']:
                    return response.result()
                else:
                    self.results.append({"id": task['id'], "result": response.result()})
            except Exception as e:
                print(f"{task['id']}, {e}")
        return None
        
