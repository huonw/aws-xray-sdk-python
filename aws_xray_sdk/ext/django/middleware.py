import logging

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core.models import http
from aws_xray_sdk.core.utils import stacktrace
from aws_xray_sdk.ext.util import calculate_sampling_decision, \
    calculate_segment_name, construct_xray_header, prepare_response_header
from aws_xray_sdk.core.lambda_launcher import check_in_lambda


log = logging.getLogger(__name__)

# Django will rewrite some http request headers.
USER_AGENT_KEY = 'HTTP_USER_AGENT'
X_FORWARDED_KEY = 'HTTP_X_FORWARDED_FOR'
REMOTE_ADDR_KEY = 'REMOTE_ADDR'
HOST_KEY = 'HTTP_HOST'
CONTENT_LENGTH_KEY = 'content-length'


class XRayMiddleware(object):
    """
    Middleware that wraps each incoming request to a segment.
    """
    def __init__(self, get_response):

        self.get_response = get_response
        self.in_lambda = False

        if check_in_lambda():
            self.in_lambda = True

    # hooks for django version >= 1.10
    def __call__(self, request):

        sampling_decision = None
        meta = request.META
        xray_header = construct_xray_header(meta)
        # a segment name is required
        name = calculate_segment_name(meta.get(HOST_KEY), xray_recorder)

        sampling_req = {
            'host': meta.get(HOST_KEY),
            'method': request.method,
            'path': request.path,
            'service': name,
        }
        sampling_decision = calculate_sampling_decision(
            trace_header=xray_header,
            recorder=xray_recorder,
            sampling_req=sampling_req,
        )

        if self.in_lambda:
            segment = xray_recorder.begin_subsegment(name)
        else:
            segment = xray_recorder.begin_segment(
                name=name,
                traceid=xray_header.root,
                parent_id=xray_header.parent,
                sampling=sampling_decision,
            )

        segment.save_origin_trace_header(xray_header)
        segment.put_http_meta(http.URL, request.build_absolute_uri())
        segment.put_http_meta(http.METHOD, request.method)

        if meta.get(USER_AGENT_KEY):
            segment.put_http_meta(http.USER_AGENT, meta.get(USER_AGENT_KEY))
        if meta.get(X_FORWARDED_KEY):
            # X_FORWARDED_FOR may come from untrusted source so we
            # need to set the flag to true as additional information
            segment.put_http_meta(http.CLIENT_IP, meta.get(X_FORWARDED_KEY))
            segment.put_http_meta(http.X_FORWARDED_FOR, True)
        elif meta.get(REMOTE_ADDR_KEY):
            segment.put_http_meta(http.CLIENT_IP, meta.get(REMOTE_ADDR_KEY))

        response = self.get_response(request)
        segment.put_http_meta(http.STATUS, response.status_code)

        if response.has_header(CONTENT_LENGTH_KEY):
            length = int(response[CONTENT_LENGTH_KEY])
            segment.put_http_meta(http.CONTENT_LENGTH, length)
        response[http.XRAY_HEADER] = prepare_response_header(xray_header, segment)

        if self.in_lambda:
            xray_recorder.end_subsegment()
        else:
            xray_recorder.end_segment()

        return response

    def process_exception(self, request, exception):
        """
        Add exception information and fault flag to the
        current segment.
        """
        segment = xray_recorder.current_segment()
        segment.put_http_meta(http.STATUS, 500)

        stack = stacktrace.get_stacktrace(limit=xray_recorder._max_trace_back)
        segment.add_exception(exception, stack)
