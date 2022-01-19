"""
ELG client.
"""
import logging
import json
import time
import requests
from requests.auth import HTTPBasicAuth

from gatenlp.processing.annotator import Annotator
from gatenlp.utils import init_logger
from gatenlp.offsetmapper import OffsetMapper

class ElgTextAnnotator(Annotator):
    # TODO: maybe we should eventually always use the elg package and the elg Service class!
    # TODO: however, currently their way how handling auth is done is too limiting see issues #8, #9

    # TODO: use template and return the URL from a method or use elg.utils
    ELG_SC_LIVE_URL_PREFIX = "https://live.european-language-grid.eu/auth/realms/ELG/protocol/openid-connect/auth?"
    ELG_SC_LIVE_URL_PREFIX += (
        "client_id=python-sdk&redirect_uri=urn:ietf:wg:oauth:2.0:oob&response_type=code"
    )
    ELG_SC_LIVE_URL_OFFLINE = ELG_SC_LIVE_URL_PREFIX + "&scope=offline_access"
    ELG_SC_LIVE_URL_OPENID = ELG_SC_LIVE_URL_PREFIX + "&scope=openid"

    ELG_SC_DEV_URL_PREFIX = "https://dev.european-language-grid.eu/auth/realms/ELG/protocol/openid-connect/auth?"
    ELG_SC_DEV_URL_PREFIX += (
        "client_id=python-sdk&redirect_uri=urn:ietf:wg:oauth:2.0:oob&response_type=code"
    )
    ELG_SC_DEV_URL_OFFLINE = ELG_SC_DEV_URL_PREFIX + "&scope=offline_access"
    ELG_SC_DEV_URL_OPENID = ELG_SC_DEV_URL_PREFIX + "&scope=openid"
    """
    An annotator that sends text to one of the services registered with the European Language Grid
    (https://live.european-language-grid.eu/) and uses the result to create annotations.

    NOTE: This is maybe not properly implemented and not properly tested yet!
    """

    def __init__(
        self,
        url=None,
        service=None,
        auth=None,
        auth_file=None,
        success_code=None,
        access_token=None,
        refresh_access=False,
        outset_name="",
        min_delay_ms=501,
        anntypes_map=None,
    ):
        """
        Create an ElgTextAnnotator.

        NOTE: error handling is not properly implemented yet since we do not know yet how exactly the various
        error conditions are represented in the result returned from the ELG services. For now, any error will
        throw an exception when `__call__` is invoked.

        NOTE: initialization can fail with an exception if success_code is specified and retrieving the
        authentification information fails.

        Args:
            url:  the annotation service URL to use. If not specified, the service parameter must be specified.
            service: the ELG service number or a tuple (servicenumber, domain). This requires the elg package.
                This may raise an exception. If successful, the url and service_meta attributes are set.
            auth: a pre-initialized ELG Authentication object. Requires the elg package. If not specified, the
                success_code or access_token parameter must be specified for authentication, or none of those
                to use an ELG-like endpoint without required authentication.
            success_code: the success code returned from the ELG web page for one of the URLs to obtain
                success codes. This will try to obtain the authentication information and store it in the
                `auth` attribute.  Requires the elg package.
                To obtain a success code, go the the ELG_SC_LIVE_URL_OPENID or ELG_SC_LIVE_URL_OFFLINE url
                and log in with your ELG user id, this will show the success code that can be copy-pasted.
            access_token: the access token token for the ELG service. Only used if auth or success_code are not
                specified. The access token is probably only valid for a limited amount of time. No refresh
                will be done and once the access token is invalid, calling `__call__` will fail with an exception.
                The access token can be obtained using the elg package or copied from the "Code samples" tab
                on the web page for a service after logging in.
            refresh_access: if True, will try to refresh the access token if auth or success_code was specified and
                refreshing is possible. Ignored if only access_token was specified
            outset_name: the name of the annotation set where to create the annotations (default: "")
            min_delay_ms: the minimum delay time between requests in milliseconds (default: 501 ms)
            anntypes_map: a map for renaming the annotation type names from the service to the ones to use in
               the annotated document.
        """
        if [x is not None for x in [url, service]].count(True) != 1:
            raise Exception("Exactly one of service or url must be specified")
        if [x is not None for x in [auth, success_code, access_token]].count(True) > 1:
            raise Exception(
                "None or exactly one of auth, success_code, or access_token must be specified"
            )
        self.access_token = access_token
        self.success_code = success_code
        self.auth = auth
        self.url = url
        self.service = service
        self.service_meta = None
        self.refresh_access = refresh_access
        # first check if we need to import the elg package
        import_elg = False
        if access_token:
            self.refresh_access = False
        if service is not None:
            import_elg = True
        if auth or success_code or auth_file:
            import_elg = True
        if import_elg:
            try:
                from elg import Authentication
                from elg.utils import get_domain, get_metadatarecord
            except Exception as ex:
                raise Exception(
                    "For this gatenlp must be installed with extra elg or extra all, e.g. gatenlp[elg]",
                    ex,
                )
        if service is not None:
            # update this to use the new method:
            # https://gitlab.com/european-language-grid/platform/python-client/-/issues/9
            if isinstance(service, tuple):
                service_id, domain = service
            else:
                service_id = service
                domain = get_domain("live")
            self.service_meta = get_metadatarecord(service_id, domain)
            # NOTE: there is also elg_execution_location for async requests!
            self.url = self.service_meta["service_info"]["elg_execution_location_sync"]
        if success_code is not None:
            self.auth = Authentication.from_success_code(success_code, domain="live")
        if auth_file is not None:
            self.auth = Authentication.from_json(auth_file)
        if self.auth:
            self.access_token = self.auth.access_token
        self.min_delay_s = min_delay_ms / 1000.0
        self.anntypes_map = anntypes_map
        self.outset_name = outset_name
        self.logger = init_logger(__name__)
        # self.logger.setLevel(logging.DEBUG)
        self._last_call_time = 0

    def __call__(self, doc, **kwargs):
        # if necessary and possible, refresh the access token
        if self.refresh_access and self.auth:
            self.auth.refresh_if_needed()
        delay = time.time() - self._last_call_time
        if delay < self.min_delay_s:
            time.sleep(self.min_delay_s - delay)
        om = OffsetMapper(doc.text)
        request_json = json.dumps(
            {"type": "text", "content": doc.text, "mimeType": "text/plain"}
        )
        hdrs = {"Content-Type": "application/json"}
        if self.access_token:
            hdrs["Authorization"] = f"Bearer {self.access_token}"
        response = requests.post(self.url, data=request_json, headers=hdrs)
        scode = response.status_code
        if scode != 200:
            raise Exception(
                f"Something went wrong, received status code/text {scode} / {response.text}"
            )
        #print(f"Response encoding:", response.encoding)
        assert response.encoding.lower() == "utf-8"
        #print(f"Response headers:", response.headers)
        #print(f"Response status code:", response.status_code)
        assert response.status_code == 200
        #print(f"Response text:", response.text)
        response_json = response.json()
        # self.logger.info(f"Response JSON: {response_json}")
        # TODO: check that we have got
        # - a map
        # - which has the "response" key
        # - response value is a map which has "type"= "annotations" and
        # - "annotations" is a map with keys being the annotation types and values arrays of annoations
        ents = response_json.get("response", {}).get("annotations", {})
        annset = doc.annset(self.outset_name)
        for ret_anntype, ret_anns in ents.items():
            if self.anntypes_map:
                anntype = self.anntypes_map.get(ret_anntype, ret_anntype)
            else:
                anntype = ret_anntype
            for ret_ann in ret_anns:
                start = ret_ann["start"]
                end = ret_ann["end"]
                feats = ret_ann.get("features", {})
                # start, end = om.convert_to_python([start, end])
                annset.add(start, end, anntype, features=feats)
        return doc
