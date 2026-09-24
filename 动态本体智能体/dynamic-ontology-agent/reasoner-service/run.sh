#!/usr/bin/env bash
# 启动推理服务。首次需先 build：mvn -B package -DskipTests
set -euo pipefail
cd "$(dirname "$0")"

JAR=target/reasoner-service.jar
if [ ! -f "$JAR" ]; then
  echo "jar 不存在，先构建..."
  mvn -B package -DskipTests
fi

exec java -jar "$JAR"
