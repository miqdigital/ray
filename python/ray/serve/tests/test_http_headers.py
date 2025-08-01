import asyncio
import uuid
from typing import Any, Dict, Optional, Tuple

import aiohttp
import httpx
import pytest
import starlette
from aiohttp import ClientSession, TCPConnector
from fastapi import FastAPI

import ray
from ray import serve
from ray.serve._private.constants import SERVE_HTTP_REQUEST_ID_HEADER
from ray.serve._private.test_utils import get_application_url
from ray.serve._private.utils import generate_request_id


def test_request_id_header_by_default(serve_instance):
    """Test that a request_id is generated by default and returned as a header."""

    @serve.deployment
    class Model:
        def __call__(self):
            request_id = ray.serve.context._get_serve_request_context().request_id
            return request_id

    serve.run(Model.bind())
    resp = httpx.get(f"{get_application_url()}")
    assert resp.status_code == 200
    assert resp.text == resp.headers[SERVE_HTTP_REQUEST_ID_HEADER]

    def is_valid_uuid(num: str):
        try:
            uuid.UUID(num, version=4)
            return True
        except ValueError:
            return False

    assert is_valid_uuid(resp.text)


class TestUserProvidedRequestIDHeader:
    def verify_result(self):
        for header_attr in ["X-Request-ID"]:
            resp = httpx.get(
                f"{get_application_url()}", headers={header_attr: "123-234"}
            )
            assert resp.status_code == 200
            assert resp.json() == 1
            assert resp.headers[header_attr] == "123-234"

    def test_basic(self, serve_instance):
        @serve.deployment
        class Model:
            def __call__(self) -> int:
                request_id = ray.serve.context._get_serve_request_context().request_id
                assert request_id == "123-234"
                return 1

        serve.run(Model.bind())
        self.verify_result()

    def test_fastapi(self, serve_instance):
        app = FastAPI()

        @serve.deployment
        @serve.ingress(app)
        class Model:
            @app.get("/")
            def say_hi(self) -> int:
                request_id = ray.serve.context._get_serve_request_context().request_id
                assert request_id == "123-234"
                return 1

        serve.run(Model.bind())
        self.verify_result()

    def test_starlette_resp(self, serve_instance):
        @serve.deployment
        class Model:
            def __call__(self) -> int:
                request_id = ray.serve.context._get_serve_request_context().request_id
                assert request_id == "123-234"
                return starlette.responses.Response("1", media_type="application/json")

        serve.run(Model.bind())
        self.verify_result()


def test_set_request_id_headers_with_two_attributes(serve_instance):
    """Test that request id is set with X-Request-ID and RAY_SERVE_REQUEST_ID.
    x-request-id has higher priority.
    """

    @serve.deployment
    class Model:
        def __call__(self):
            request_id = ray.serve.context._get_serve_request_context().request_id
            return request_id

    serve.run(Model.bind())
    resp = httpx.get(
        get_application_url(),
        headers={
            "X-Request-ID": "234",
        },
    )

    assert resp.status_code == 200
    assert SERVE_HTTP_REQUEST_ID_HEADER in resp.headers
    assert resp.text == resp.headers[SERVE_HTTP_REQUEST_ID_HEADER]


def test_reuse_request_id(serve_instance):
    """Test client re-uses request id.

    When multiple requests are submitted with the same request id at around the same
    time, the proxy should continue to track the correct request objects, setting
    the correct request id in the serve context, and return the original x-request-id
    request header as the response header.

    For more details, see https://github.com/ray-project/ray/issues/45723.
    """

    app = FastAPI()

    @serve.deployment(num_replicas=3)
    @serve.ingress(app)
    class MyFastAPIDeployment:
        @app.post("/hello")
        def root(self, user_input: Dict[str, str]) -> Dict[str, str]:
            request_id = ray.serve.context._get_serve_request_context().request_id
            return {
                "app_name": user_input["app_name"],
                "serve_context_request_id": request_id,
            }

    serve.run(MyFastAPIDeployment.bind())

    async def send_request(
        session: ClientSession, body: Dict[str, Any], request_id: Optional[str]
    ) -> Tuple[str, str]:
        headers = {SERVE_HTTP_REQUEST_ID_HEADER: request_id}
        url = "http://localhost:8000/hello"

        async with session.post(url=url, headers=headers, json=body) as response:
            result = await response.json()
            # Ensure the request object is tracked correctly.
            assert result["app_name"] == body["app_name"]
            # Ensure the request id from the serve context is set correctly.
            assert result["serve_context_request_id"] == request_id
            # Ensure the request id from the response header is returned correctly.
            assert response.headers[SERVE_HTTP_REQUEST_ID_HEADER] == request_id

    async def main():
        """Sending 20 requests in parallel all with the same request id, but with
        different request body.
        """
        bodies = [{"app_name": f"an_{generate_request_id()}"} for _ in range(20)]
        connector = TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            request_id = f"rid_{generate_request_id()}"
            tasks = [
                send_request(session, body, request_id=request_id) for body in bodies
            ]
            await asyncio.gather(*tasks)

    asyncio.run(main())


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", "-s", __file__]))
