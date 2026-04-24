"""Tests for /internal/dashboard → /api routing and async poll (/api/get) behavior."""

from unittest import mock

import fastapi
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sky.server.requests import requests as requests_lib
from sky.server.server import InternalDashboardPrefixMiddleware
from sky.server.server import api_get


async def _echo_scope_path(request: Request):
    return JSONResponse({'path': request.url.path, 'raw_path': request.scope.get('path')})


def test_internal_dashboard_prefix_middleware_rewrites_path():
    """Maestro and the dashboard poll use /internal/dashboard/api/get.

    The middleware must strip the prefix so routing matches /api/get. Inner
    handlers should observe the rewritten path (not the original URL).
    """
    app = Starlette(routes=[Route('/api/get', _echo_scope_path, methods=['GET'])])
    app.add_middleware(InternalDashboardPrefixMiddleware)
    client = TestClient(app)
    response = client.get('/internal/dashboard/api/get?request_id=x')
    assert response.status_code == 200
    body = response.json()
    assert body['path'] == '/api/get'
    assert body['raw_path'] == '/api/get'


@pytest.mark.asyncio
async def test_api_get_returns_500_when_async_request_failed():
    """When the scheduled job fails, /api/get responds with HTTP 500.

    Clients (e.g. Maestro sync-config) poll GET .../api/get?request_id= until
    the job leaves PENDING/RUNNING; if the worker recorded an error, the poll
    ends with 500 and a JSON body describing the failed RequestPayload. This is
    long-standing server behavior, not a transport error.
    """
    request_id = '11111111-1111-1111-1111-111111111111'
    mock_task = mock.MagicMock()
    mock_task.should_retry = False
    mock_task.get_error.return_value = {
        'object': ValueError('invalid config'),
        'type': 'ValueError',
        'message': 'invalid config',
    }
    encoded = mock.MagicMock()
    encoded.model_dump.return_value = {
        'request_id': request_id,
        'status': requests_lib.RequestStatus.FAILED.value,
    }
    mock_task.encode.return_value = encoded

    with mock.patch(
            'sky.server.server.get_expanded_request_id',
            new=mock.AsyncMock(return_value=request_id)), \
         mock.patch(
             'sky.server.server.requests_lib.get_request_status_async',
             new=mock.AsyncMock(
                 return_value=requests_lib.StatusWithMsg(
                     requests_lib.RequestStatus.FAILED))), \
         mock.patch(
             'sky.server.server.requests_lib.get_request_async',
             new=mock.AsyncMock(return_value=mock_task)):
        with pytest.raises(fastapi.HTTPException) as exc_info:
            await api_get(request_id)
    assert exc_info.value.status_code == 500
    detail = exc_info.value.detail
    assert detail['request_id'] == request_id
    assert detail['status'] == requests_lib.RequestStatus.FAILED.value
