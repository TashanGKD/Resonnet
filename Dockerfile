ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
ARG NODE_BASE_IMAGE=docker.m.daocloud.io/library/node:20-slim
FROM ${NODE_BASE_IMAGE} AS node-runtime
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ARG PIP_TIMEOUT=120
ARG PIP_RETRIES=3
ARG APT_MIRROR=http://mirrors.aliyun.com/debian
ARG APT_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security
ARG NPM_REGISTRY=https://registry.npmmirror.com

# 创建非 root 用户
RUN useradd -m -u 1000 appuser

WORKDIR /app

# 复用 Node 镜像中的运行时，避免通过 NodeSource 下载 Node.js。
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    node --version && npm --version

# 安装 sandbox-runtime (srt) 运行时依赖
# srt 在 Linux 上需要: bubblewrap, socat, ripgrep
RUN for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
      [ ! -f "$source_file" ] || sed -i \
        -e "s|https\?://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|https\?://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|https\?://deb.debian.org/debian|${APT_MIRROR}|g" \
        "$source_file"; \
    done && \
    printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Pipeline-Depth "0";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        > /etc/apt/apt.conf.d/80-topiclab-mirror && \
    apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap socat ripgrep ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/80-topiclab-mirror

RUN npm config set registry "${NPM_REGISTRY}" && \
    npm config set fetch-retries 5 && \
    npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    npm config set fetch-timeout 300000 && \
    npm install -g @anthropic-ai/sandbox-runtime && \
    srt --version

# 配置 pip 下载源；CI 和海外部署可通过 build args 切换到官方 PyPI。
RUN mkdir -p /home/appuser/.pip && \
    echo "[global]" > /home/appuser/.pip/pip.conf && \
    echo "index-url = ${PIP_INDEX_URL}" >> /home/appuser/.pip/pip.conf && \
    echo "trusted-host = ${PIP_TRUSTED_HOST}" >> /home/appuser/.pip/pip.conf && \
    echo "timeout = ${PIP_TIMEOUT}" >> /home/appuser/.pip/pip.conf && \
    echo "retries = ${PIP_RETRIES}" >> /home/appuser/.pip/pip.conf

# 复制代码并修改所有权
COPY . .
# 保留内置 libs 到 libs_builtin，供挂载 LIBS_PATH 为空时初始化
RUN cp -r libs libs_builtin 2>/dev/null || true
RUN chown -R appuser:appuser /app /home/appuser/.pip

# 切换到非 root 用户
USER appuser

# 添加用户本地 bin 目录到 PATH
ENV PATH="/home/appuser/.local/bin:$PATH"

# 安装依赖（使用构建时选择的镜像源）
RUN for i in 1 2 3; do \
      pip install --no-cache-dir -e . && exit 0; \
      echo "pip attempt $i failed, retrying in 20s..."; \
      sleep 20; \
    done; \
    exit 1

EXPOSE 8000

# 工作区目录，运行时需挂载 volume 持久化数据
ENV WORKSPACE_BASE=/app/workspace

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
