ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

# 创建非 root 用户
RUN useradd -m -u 1000 appuser

WORKDIR /app

# 安装 sandbox-runtime (srt) 运行时依赖和 Node.js
# srt 在 Linux 上需要: bubblewrap, socat, ripgrep
# Node.js 用于运行 @anthropic-ai/sandbox-runtime CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap socat ripgrep curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/sandbox-runtime \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 配置 pip 下载源；CI 和海外部署可通过 build args 切换到官方 PyPI。
RUN mkdir -p /home/appuser/.pip && \
    echo "[global]" > /home/appuser/.pip/pip.conf && \
    echo "index-url = ${PIP_INDEX_URL}" >> /home/appuser/.pip/pip.conf && \
    echo "trusted-host = ${PIP_TRUSTED_HOST}" >> /home/appuser/.pip/pip.conf && \
    echo "timeout = 300" >> /home/appuser/.pip/pip.conf && \
    echo "retries = 10" >> /home/appuser/.pip/pip.conf

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
RUN for i in 1 2 3; do pip install --no-cache-dir -e . && break || { echo "pip attempt $i failed, retrying in 20s..."; sleep 20; }; done

EXPOSE 8000

# 工作区目录，运行时需挂载 volume 持久化数据
ENV WORKSPACE_BASE=/app/workspace

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
