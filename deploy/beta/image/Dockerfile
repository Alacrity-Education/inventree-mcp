# The fork's patched InvenTree (master + the inventree-mcp core patches) with the plugin baked in, so the server and
# the worker containers run the same code without installing anything at start. A wheel placed next to this file
# (image/*.whl, git-ignored) wins over PLUGIN_WHEEL, for testing a build before its release.
ARG BASE_IMAGE=ghcr.io/alacrity-education/inventree:1.6.0-alacrity.1
FROM ${BASE_IMAGE}
ARG PLUGIN_WHEEL=https://github.com/Alacrity-Education/inventree-mcp/releases/download/0.4.1/inventree_mcp-0.4.1-py3-none-any.whl
COPY . /tmp/image/
RUN set -e; \
    if ls /tmp/image/*.whl >/dev/null 2>&1; then src="$(ls /tmp/image/*.whl | head -1)"; else src="${PLUGIN_WHEEL}"; fi; \
    echo "installing ${src}"; \
    python3 -m pip install --no-cache-dir --disable-pip-version-check "${src}" \
    && python3 -c "import inventree_mcp; print('inventree-mcp', inventree_mcp.PLUGIN_VERSION)" \
    && rm -rf /tmp/image
