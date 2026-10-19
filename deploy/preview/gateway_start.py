"""Start the operator-authenticated gateway from configured non-secret upstreams."""
import os
import uvicorn
from control_plane.preview_gateway import GatewayConfig, create_gateway
uvicorn.run(create_gateway(GatewayConfig(
    os.environ['HO_PREVIEW_HERMES_URL'], os.environ['HO_PREVIEW_APPLICATION_URL'],
    os.environ['HO_PREVIEW_PUBLIC_ORIGIN'], os.environ['HO_PREVIEW_OPERATOR_ID'],
    odoo_password_file=os.environ.get('HO_PREVIEW_ODOO_PASSWORD_FILE'),
    version_file=os.environ.get('HO_PREVIEW_VERSION_FILE'),
    dashboard_url=os.environ.get('HO_PREVIEW_DASHBOARD_URL', '/'),
    api_url=os.environ.get('HO_PREVIEW_API_URL'),
    plugin_token_file=os.environ.get('HO_PREVIEW_PLUGIN_TOKEN_FILE'),
    websocket_url=os.environ.get('HO_PREVIEW_WEBSOCKET_URL'),
)), host='0.0.0.0', port=8085)
