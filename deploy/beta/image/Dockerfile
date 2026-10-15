# The fork's patched InvenTree (master + the inventree-mcp core patches) with the plugin baked in, so the server and
# the worker containers run the same code without installing anything at start.
ARG BASE_IMAGE=ghcr.io/alacrity-education/inventree:1.6.0-alacrity.1
FROM ${BASE_IMAGE}
ARG PLUGIN_WHEEL=https://github.com/Alacrity-Education/inventree-mcp/releases/download/0.4.0/inventree_mcp-0.4.0-py3-none-any.whl
RUN python3 -m pip install --no-cache-dir --disable-pip-version-check "${PLUGIN_WHEEL}" \
    && python3 -c "import inventree_mcp; print('inventree-mcp', inventree_mcp.PLUGIN_VERSION)"
