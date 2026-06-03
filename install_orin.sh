#!/usr/bin/env bash
set -euo pipefail

ORIN_INSTALLER_VERSION="0.1.0"

usage() {
  cat <<'EOF'
Orin installer

Usage:
  install-orin.sh [options]

Common:
  --name <name>              Host alias, host name, and k3s node name unless overridden
  --host-name <name>         Host name stored in Orin configuration
  --host-ip <ip>             Host IP stored in Orin configuration
  --k3s-node-name <name>     Kubernetes node name
  --namespace <name>         Kubernetes namespace, default: orin

Platform:
  --platform <value>         auto|amd64|arm64|jetson, default: auto
  --gpu                     Force NVIDIA GPU true
  --no-gpu                  Force NVIDIA GPU false

Images:
  --registry <host:port>     Image registry, default: orin-gw:5000
  --image-version <tag>      Image tag, default: 1.0.0

SSH:
  --ssh-user <user>          SSH user for future node operations, default: ubuntu
  --ssh-key <path>           SSH private key to mount into Cortex, default: ~/.ssh/id_ed25519
  --ssh-port <port>          SSH port, default: 22

Network:
  --node-port <port>         Cortex NodePort, default: 30080

Registry:
  --local-registry           Configure local/insecure registry for k3s, default
  --no-local-registry        Do not configure local registry
  --local-registry-endpoint <url>

Mode:
  -y, --yes                  Non-interactive mode; accept detected/default values
  -h, --help                 Show this help

Examples:
  curl -fsSL <installer-url> | bash

  curl -fsSL <installer-url> | bash -s -- \
    --name orin-gw \
    --platform jetson \
    --gpu \
    --registry orin-gw:5000

EOF
}

detect_invoking_home() {
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    getent passwd "$SUDO_USER" | cut -d: -f6
    return
  fi

  printf '%s\n' "$HOME"
}

ORIN_NAMESPACE="orin"

HOST_ALIAS=""
HOST_NAME=""
HOST_IP=""
K3S_NODE_NAME=""

SSH_USER="ubuntu"
INVOKING_HOME="$(detect_invoking_home)"
SSH_KEY="${SSH_KEY:-${INVOKING_HOME}/.ssh/id_ed25519}"
SSH_KEY_MOUNT_PATH="/data/ssh/id_ed25519"
SSH_KEY_SECRET_NAME="cortex-ssh-key"
SSH_PORT="22"

PLATFORM="auto"
NVIDIA_GPU="auto"

CORTEX_SIZE="light"
CORTEX_NODE_PORT="30080"
CORTEX_SERVICE_ACCOUNT="cortex"

REGISTRY="orin-gw:5000"
IMAGE_VERSION="1.0.0"

CONFIG_PATH="/data/configuration"
K3S_DATA_DIR="/data/k3s"
K3S_STORAGE_DIR="/data/k3s-storage"

CONFIGURE_LOCAL_REGISTRY="true"
LOCAL_REGISTRY_ENDPOINT=""

ASSUME_YES="false"

FEDERATION_NODE_PORT="30081"
FEDERATION_PVC_SIZE="1Gi"


parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --name)
        HOST_ALIAS="${2:?missing value for --name}"
        HOST_NAME="$HOST_ALIAS"
        K3S_NODE_NAME="$HOST_ALIAS"
        shift 2
        ;;

      --host-name)
        HOST_NAME="${2:?missing value for --host-name}"
        shift 2
        ;;

      --host-ip)
        HOST_IP="${2:?missing value for --host-ip}"
        shift 2
        ;;

      --k3s-node-name)
        K3S_NODE_NAME="${2:?missing value for --k3s-node-name}"
        shift 2
        ;;

      --namespace)
        ORIN_NAMESPACE="${2:?missing value for --namespace}"
        shift 2
        ;;

      --platform)
        PLATFORM="${2:?missing value for --platform}"
        case "$PLATFORM" in
          auto|amd64|arm64|jetson) ;;
          *)
            echo "Invalid --platform: $PLATFORM"
            echo "Expected: auto, amd64, arm64, jetson"
            exit 1
            ;;
        esac
        shift 2
        ;;

      --gpu)
        NVIDIA_GPU="true"
        shift
        ;;

      --no-gpu)
        NVIDIA_GPU="false"
        shift
        ;;

      --registry)
        REGISTRY="${2:?missing value for --registry}"
        shift 2
        ;;

      --image-version)
        IMAGE_VERSION="${2:?missing value for --image-version}"
        shift 2
        ;;

      --ssh-user)
        SSH_USER="${2:?missing value for --ssh-user}"
        shift 2
        ;;

      --ssh-key)
        SSH_KEY="${2:?missing value for --ssh-key}"
        shift 2
        ;;

      --ssh-port)
        SSH_PORT="${2:?missing value for --ssh-port}"
        shift 2
        ;;

      --node-port)
        CORTEX_NODE_PORT="${2:?missing value for --node-port}"
        shift 2
        ;;

      --local-registry)
        CONFIGURE_LOCAL_REGISTRY="true"
        shift
        ;;

      --no-local-registry)
        CONFIGURE_LOCAL_REGISTRY="false"
        shift
        ;;

      --local-registry-endpoint)
        LOCAL_REGISTRY_ENDPOINT="${2:?missing value for --local-registry-endpoint}"
        shift 2
        ;;

      -y|--yes)
        ASSUME_YES="true"
        shift
        ;;

      -h|--help)
        usage
        exit 0
        ;;

      *)
        echo "Unknown option: $1"
        echo
        usage
        exit 1
        ;;
    esac
  done

  if [ -z "$LOCAL_REGISTRY_ENDPOINT" ]; then
    LOCAL_REGISTRY_ENDPOINT="http://${REGISTRY}"
  fi
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "This installer must run as root."
    echo
    echo "Use:"
    echo "  sudo ./install_orin.sh [options]"
    echo
    echo "or:"
    echo "  curl -fsSL <installer-url> | sudo bash -s -- [options]"
    exit 1
  fi
}

detect_host_name() {
  if command -v hostname >/dev/null 2>&1; then
    hostname
    return
  fi

  if command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl --static 2>/dev/null && return
  fi

  if command -v uname >/dev/null 2>&1; then
    uname -n
    return
  fi

  if [ -r /etc/hostname ]; then
    head -n 1 /etc/hostname
    return
  fi

  echo "orin-node"
}

detect_host_ip() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '
          {
            for (i = 1; i <= NF; i++) {
              if ($i == "src") {
                print $(i + 1)
                exit
              }
            }
          }
        ' && return
  fi

  echo "127.0.0.1"
}

resolve_host_defaults() {
  local detected_name
  local detected_ip
  local invoking_home

  detected_name="$(detect_host_name)"
  detected_ip="$(detect_host_ip)"
  invoking_home="$(detect_invoking_home)"

  if [ -z "$HOST_ALIAS" ]; then
    HOST_ALIAS="$detected_name"
  fi

  if [ -z "$HOST_NAME" ]; then
    HOST_NAME="$HOST_ALIAS"
  fi

  if [ -z "$HOST_IP" ]; then
    HOST_IP="$detected_ip"
  fi

  if [ -z "$K3S_NODE_NAME" ]; then
    K3S_NODE_NAME="$HOST_ALIAS"
  fi

  if [ -z "$SSH_KEY" ]; then
    SSH_KEY="${invoking_home}/.ssh/id_ed25519"
  fi
}

confirm_install() {
  cat <<EOF

Orin install plan

  namespace:       ${ORIN_NAMESPACE}
  host alias:      ${HOST_ALIAS}
  host name:       ${HOST_NAME}
  host ip:         ${HOST_IP}
  k3s node name:   ${K3S_NODE_NAME}

  platform:        ${DETECTED_PLATFORM}
  nvidia gpu:      ${DETECTED_GPU}

  registry:        ${REGISTRY}
  image version:   ${IMAGE_VERSION}

  ssh user:        ${SSH_USER}
  ssh key:         ${SSH_KEY}

  cortex endpoint: http://${HOST_IP}:${CORTEX_NODE_PORT}

EOF

  if [ "$ASSUME_YES" = "true" ]; then
    return
  fi

  if [ ! -r /dev/tty ]; then
    echo "No interactive terminal available."
    echo "Re-run with --yes or pass explicit flags."
    exit 1
  fi

  printf "Continue? [y/N] " > /dev/tty
  read -r answer < /dev/tty

  case "$answer" in
    y|Y|yes|YES) ;;
    *)
      echo "Install cancelled."
      exit 0
      ;;
  esac
}

require_root_tools() {
  if ! command -v curl >/dev/null 2>&1; then
    apt-get update
    apt-get install -y curl ca-certificates
  fi
}

detect_platform() {
  if [ "$PLATFORM" != "auto" ]; then
    echo "$PLATFORM"
    return
  fi

  local arch
  arch="$(uname -m)"

  if [ -r /proc/device-tree/model ] && tr -d '\0' < /proc/device-tree/model | grep -Eqi 'jetson|tegra|nvidia'; then
    echo "jetson"
    return
  fi

  case "$arch" in
    x86_64)
      echo "amd64"
      ;;
    aarch64|arm64)
      echo "arm64"
      ;;
    *)
      echo "unsupported"
      ;;
  esac
}

detect_gpu() {
  if [ "$NVIDIA_GPU" != "auto" ]; then
    echo "$NVIDIA_GPU"
    return
  fi

  if [ "$1" = "jetson" ]; then
    echo "true"
    return
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "true"
    return
  fi

  echo "false"
}

image_for() {
  local app="$1"
  local platform="$2"

  case "$platform" in
    jetson)
      echo "${REGISTRY}/${app}-jetson:${IMAGE_VERSION}"
      ;;
    arm64)
      echo "${REGISTRY}/${app}-arm64:${IMAGE_VERSION}"
      ;;
    amd64)
      echo "${REGISTRY}/${app}:${IMAGE_VERSION}"
      ;;
    *)
      echo "unsupported"
      ;;
  esac
}

write_configuration() {
  local platform="$1"
  local nvidia_gpu="$2"

  local primer_n_gpu_layer=0
  if [ "$nvidia_gpu" = "true" ]; then
    primer_n_gpu_layer=-1
  fi

  mkdir -p "$(dirname "$CONFIG_PATH")"

  tee "$CONFIG_PATH" >/dev/null <<EOF
{
  "namespace": "${ORIN_NAMESPACE}",
  "alias": "${HOST_ALIAS}",
  "host": "${HOST_NAME}",
  "ip": "${HOST_IP}",
  "ssh_key": "${SSH_KEY_MOUNT_PATH}",
  "user": "${SSH_USER}",
  "port": ${SSH_PORT},
  "nvidia_gpu": ${nvidia_gpu},
  "platform": "${platform}",
  "k3s_node_name": "${K3S_NODE_NAME}",
  "in_cluster": true,

  "images": {
    "registry": "${REGISTRY}",
    "version": "${IMAGE_VERSION}"
  },

  "deployment": {
    "default_port": 8080,
    "cortex_node_port": ${CORTEX_NODE_PORT},
    "federation_node_port": ${FEDERATION_NODE_PORT},
    "pvc_size": "30Gi",
    "pvc_storage_class_name": "local-path",
    "pvc_mount_path": "/models/huggingface",
    "image_pull_policy": "Always",
    "runtime_class_name": "nvidia",
    "cortex_service_account_name": "${CORTEX_SERVICE_ACCOUNT}"
  },

  "primer": {
    "backend": "",
    "model_id": "",
    "model_path": "",
    "tokenizer_path": "",
    "max_new_tokens": 1400,
    "temperature": 0.1,
    "top_p": 0.95,
    "max_model_len": 4096,
    "n_gpu_layer": ${primer_n_gpu_layer},
    "n_threads": 6,
    "n_batch": 512,
    "verbose": false,
    "gpu_memory_util": 0.50,
    "tensor_parallel_size": 1
  },

  "federation": {
    "app_name": "orin-federation",
    "protocol_version": "v1",
    "host": "0.0.0.0",
    "port": 8080,
    "cluster_id": "",
    "data_dir": "/data/federation",
    "private_key_path": "/data/federation/identity/ed25519_private.key",
    "public_key_path": "/data/federation/identity/ed25519_public.key",

    "memory_base_url": "http://memory-service.${ORIN_NAMESPACE}.svc.cluster.local:8080",
    "memory_work_path": "/work",

    "public_base_url": "http://${HOST_IP}:${FEDERATION_NODE_PORT}",

    "cortex_base_url": "http://cortex-service.${ORIN_NAMESPACE}.svc.cluster.local:8080",
    "cortex_work_path": "/work",
    "cortex_work_result_path": "/work/{work_id}",
    "cortex_network_path": "/network",

    "default_network_visibility": "public",
    "default_join_mode": "token",

    "request_ttl_seconds": 300,
    "allowed_clock_skew_seconds": 60,
    "nonce_ttl_seconds": 600,
    "max_request_bytes": 1000000,

    "enable_remote_work_submission": true,
    "enable_capability_publish": true,
    "enable_member_heartbeat": true
  }
}
EOF
}

configure_registry() {
  if [ "$CONFIGURE_LOCAL_REGISTRY" != "true" ]; then
    return
  fi

  mkdir -p /etc/rancher/k3s

  tee /etc/rancher/k3s/registries.yaml >/dev/null <<EOF
mirrors:
  "${REGISTRY}":
    endpoint:
      - "${LOCAL_REGISTRY_ENDPOINT}"
configs:
  "${REGISTRY}":
    tls:
      insecure_skip_verify: true
EOF
}

install_k3s_server() {
  if systemctl is-active --quiet k3s; then
    echo "k3s is already running; skipping install"
    return
  fi

  mkdir -p "$K3S_DATA_DIR" "$K3S_STORAGE_DIR"

  local install_exec
  install_exec="server \
--node-name ${K3S_NODE_NAME} \
--data-dir ${K3S_DATA_DIR} \
--write-kubeconfig-mode 644 \
--default-local-storage-path ${K3S_STORAGE_DIR} \
--disable servicelb \
--node-label orin.role.backend=true \
--node-label orin.backend.cortex=true \
--node-label orin.backend.${CORTEX_SIZE}=true \
--node-label orin.platform=${DETECTED_PLATFORM}"

  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="$install_exec" sh -
}

kubectl_wait() {
  local retries=60

  until k3s kubectl get nodes >/dev/null 2>&1; do
    retries=$((retries - 1))

    if [ "$retries" -le 0 ]; then
      echo "Timed out waiting for Kubernetes API"
      exit 1
    fi

    sleep 2
  done

  k3s kubectl wait --for=condition=Ready node/"$K3S_NODE_NAME" --timeout=180s
}

apply_cortex_ssh_key_secret() {
  local key_path

  key_path="$(eval echo "$SSH_KEY")"

  if [ ! -f "$key_path" ]; then
    echo "SSH private key not found: $key_path"
    echo "Set SSH_KEY=/path/to/private/key before running installer."
    exit 1
  fi

   k3s kubectl -n "$ORIN_NAMESPACE" create secret generic "$SSH_KEY_SECRET_NAME" \
    --from-file=id_ed25519="$key_path" \
    --dry-run=client \
    -o yaml |  k3s kubectl apply -f -
}

apply_k3s_token_secret() {
  local token_path
  local token

  token_path="${K3S_DATA_DIR}/server/node-token"

  if [ ! -f "$token_path" ]; then
    token_path="/var/lib/rancher/k3s/server/node-token"
  fi

  if [ ! -f "$token_path" ]; then
    echo "K3s node token not found."
    echo "Checked:"
    echo "  ${K3S_DATA_DIR}/server/node-token"
    echo "  /var/lib/rancher/k3s/server/node-token"
    exit 1
  fi

  token="$( cat "$token_path")"

  if [ -z "$token" ]; then
    echo "K3s node token is empty: $token_path"
    exit 1
  fi

   k3s kubectl -n "$ORIN_NAMESPACE" create secret generic k3s-join-token \
    --from-literal=token="$token" \
    --dry-run=client \
    -o yaml |  k3s kubectl apply -f -
}

apply_runtime_class_if_needed() {
  if [ "$DETECTED_GPU" != "true" ]; then
    return
  fi

   k3s kubectl apply -f - <<EOF
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF
}

apply_namespace_and_rbac() {
   k3s kubectl create namespace "$ORIN_NAMESPACE" \
    --dry-run=client \
    -o yaml |  k3s kubectl apply -f -

   k3s kubectl -n "$ORIN_NAMESPACE" create serviceaccount "$CORTEX_SERVICE_ACCOUNT" \
    --dry-run=client \
    -o yaml |  k3s kubectl apply -f -

   k3s kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cortex-deployment-manager
rules:
  - apiGroups: [""]
    resources:
      - nodes
    verbs: ["get", "list", "watch", "patch", "update"]

  - apiGroups: [""]
    resources:
      - namespaces
    verbs: ["get", "list", "create"]

  - apiGroups: [""]
    resources:
      - pods
      - services
      - persistentvolumeclaims
      - configmaps
    verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]

  - apiGroups: [""]
    resources:
      - secrets
    verbs: ["get", "list", "watch"]

  - apiGroups: ["apps"]
    resources:
      - deployments
    verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]

  - apiGroups: ["discovery.k8s.io"]
    resources:
    - endpointslices
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cortex-deployment-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cortex-deployment-manager
subjects:
  - kind: ServiceAccount
    name: ${CORTEX_SERVICE_ACCOUNT}
    namespace: ${ORIN_NAMESPACE}
EOF
}

apply_configuration_configmap() {
   k3s kubectl -n "$ORIN_NAMESPACE" create configmap deployment-configuration \
    --from-file=configuration="$CONFIG_PATH" \
    --dry-run=client \
    -o yaml |  k3s kubectl apply -f -
}

label_host_node() {
   k3s kubectl label node "$K3S_NODE_NAME" \
    orin.role.backend=true \
    orin.backend.cortex=true \
    orin.backend.memory=true \
    orin.backend.federation=true \
    "orin.backend.${CORTEX_SIZE}=true" \
    "orin.platform=${DETECTED_PLATFORM}" \
    --overwrite
}

deploy_cortex() {
  local cortex_image
  cortex_image="$(image_for cortex "$DETECTED_PLATFORM")"

  if [ "$cortex_image" = "unsupported" ]; then
    echo "Unsupported platform for cortex image: $DETECTED_PLATFORM"
    exit 1
  fi

  local runtime_class_line=""
  if [ "$DETECTED_GPU" = "true" ]; then
    runtime_class_line="      runtimeClassName: nvidia"
  fi

   k3s kubectl -n "$ORIN_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cortex-pvc
  namespace: ${ORIN_NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 30Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cortex
  namespace: ${ORIN_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cortex
  template:
    metadata:
      labels:
        app: cortex
    spec:
${runtime_class_line}
      serviceAccountName: ${CORTEX_SERVICE_ACCOUNT}
      nodeSelector:
        orin.role.backend: "true"
        orin.backend.cortex: "true"
        orin.backend.${CORTEX_SIZE}: "true"
        orin.platform: "${DETECTED_PLATFORM}"
      enableServiceLinks: false
      containers:
        - name: cortex
          image: ${cortex_image}
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
              name: http
              protocol: TCP
          readinessProbe:
            httpGet:
              path: /ready
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 12
          livenessProbe:
            httpGet:
              path: /live
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 8
          startupProbe:
            httpGet:
              path: /ready
              port: 8080
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 120
          volumeMounts:
            - name: cortex-data
              mountPath: /models/huggingface
            - name: deployment-configuration
              mountPath: /data
              readOnly: true
            - name: cortex-ssh-key
              mountPath: /data/ssh
              readOnly: true
      volumes:
        - name: cortex-data
          persistentVolumeClaim:
            claimName: cortex-pvc
        - name: deployment-configuration
          configMap:
            name: deployment-configuration
        - name: cortex-ssh-key
          secret:
            secretName: ${SSH_KEY_SECRET_NAME}
            defaultMode: 0400
---
apiVersion: v1
kind: Service
metadata:
  name: cortex-service
  namespace: ${ORIN_NAMESPACE}
  labels:
    orin.ai/backend: "true"
    orin.ai/role: "cortex"
  annotations:
    orin.ai/kind: "cortex"
    orin.ai/role: "cortex"
    orin.ai/visibility: "internal"
spec:
  selector:
    app: cortex
  ports:
    - name: http
      port: 8080
      targetPort: 8080
      nodePort: ${CORTEX_NODE_PORT}
      protocol: TCP
  type: NodePort
EOF
}

deploy_memory() {
  local memory_image
  memory_image="$(image_for memory "$DETECTED_PLATFORM")"

  if [ "$memory_image" = "unsupported" ]; then
    echo "Unsupported platform for memory image: $DETECTED_PLATFORM"
    exit 1
  fi

  local runtime_class_line=""
  if [ "$DETECTED_GPU" = "true" ]; then
    runtime_class_line="      runtimeClassName: nvidia"
  fi

   k3s kubectl -n "$ORIN_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: memory-pvc
  namespace: ${ORIN_NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: memory
  namespace: ${ORIN_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: memory
  template:
    metadata:
      labels:
        app: memory
    spec:
${runtime_class_line}
      nodeSelector:
        orin.role.backend: "true"
      enableServiceLinks: false
      containers:
        - name: memory
          image: ${memory_image}
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
              name: http
              protocol: TCP
          readinessProbe:
            httpGet:
              path: /ready
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 12
          livenessProbe:
            httpGet:
              path: /live
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 8
          startupProbe:
            httpGet:
              path: /ready
              port: 8080
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 120
          volumeMounts:
            - name: memory-data
              mountPath: /data/memory
      volumes:
        - name: memory-data
          persistentVolumeClaim:
            claimName: memory-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: memory-service
  namespace: ${ORIN_NAMESPACE}
  labels:
    orin.ai/backend: "true"
    orin.ai/role: "memory"
  annotations:
    orin.ai/kind: "memory"
    orin.ai/role: "memory"
    orin.ai/visibility: "internal"
spec:
  selector:
    app: memory
  ports:
    - name: http
      port: 8080
      targetPort: 8080
      protocol: TCP
  type: ClusterIP
EOF
}

deploy_federation() {
  local federation_image
  federation_image="$(image_for federation "$DETECTED_PLATFORM")"

  if [ "$federation_image" = "unsupported" ]; then
    echo "Unsupported platform for federation image: $DETECTED_PLATFORM"
    exit 1
  fi

  k3s kubectl -n "$ORIN_NAMESPACE" apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: federation-pvc
  namespace: ${ORIN_NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: ${FEDERATION_PVC_SIZE}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: federation
  namespace: ${ORIN_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: federation
  template:
    metadata:
      labels:
        app: federation
    spec:
      nodeSelector:
        orin.role.backend: "true"
        orin.backend.federation: "true"
        orin.platform: "${DETECTED_PLATFORM}"
      enableServiceLinks: false
      containers:
        - name: federation
          image: ${federation_image}
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
              name: http
              protocol: TCP
          readinessProbe:
            httpGet:
              path: /ready
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 12
          livenessProbe:
            httpGet:
              path: /live
              port: 8080
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 8
          startupProbe:
            httpGet:
              path: /ready
              port: 8080
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 120
          volumeMounts:
            - name: deployment-configuration
              mountPath: /data/configuration
              subPath: configuration
              readOnly: true
            - name: federation-data
              mountPath: /data/federation
      volumes:
        - name: deployment-configuration
          configMap:
            name: deployment-configuration
        - name: federation-data
          persistentVolumeClaim:
            claimName: federation-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: federation-service
  namespace: ${ORIN_NAMESPACE}
  labels:
    app: federation
    orin.ai/role: "federation"
  annotations:
    orin.ai/kind: "federation"
    orin.ai/role: "federation"
    orin.ai/visibility: "external"
spec:
  selector:
    app: federation
  ports:
    - name: http
      port: 8080
      targetPort: 8080
      nodePort: ${FEDERATION_NODE_PORT}
      protocol: TCP
  type: NodePort
EOF
}

wait_for_memory() {
  echo "Waiting for Memory deployment to become available..."

   k3s kubectl -n "$ORIN_NAMESPACE" rollout status deployment/memory --timeout=20m || {
    echo
    echo "Memory rollout failed or timed out. Debug info:"
     k3s kubectl -n "$ORIN_NAMESPACE" get pods -l app=memory -o wide || true
     k3s kubectl -n "$ORIN_NAMESPACE" describe pod -l app=memory || true
     k3s kubectl -n "$ORIN_NAMESPACE" logs -l app=memory --tail=100 || true
    return 1
  }
}

wait_for_cortex() {
  echo "Waiting for Cortex deployment to become available..."

   k3s kubectl -n "$ORIN_NAMESPACE" get pods -l app=cortex -o wide || true

   k3s kubectl -n "$ORIN_NAMESPACE" rollout status deployment/cortex --timeout=20m || {
    echo
    echo "Cortex rollout failed or timed out. Debug info:"
     k3s kubectl -n "$ORIN_NAMESPACE" get pods -l app=cortex -o wide || true
     k3s kubectl -n "$ORIN_NAMESPACE" describe pod -l app=cortex || true
     k3s kubectl -n "$ORIN_NAMESPACE" logs -l app=cortex --tail=100 || true
    return 1
  }
}

wait_for_federation() {
  echo "Waiting for Federation deployment to become available..."

  k3s kubectl -n "$ORIN_NAMESPACE" get pods -l app=federation -o wide || true

  k3s kubectl -n "$ORIN_NAMESPACE" rollout status deployment/federation --timeout=20m || {
    echo
    echo "Federation rollout failed or timed out. Debug info:"
    k3s kubectl -n "$ORIN_NAMESPACE" get pods -l app=federation -o wide || true
    k3s kubectl -n "$ORIN_NAMESPACE" describe pod -l app=federation || true
    k3s kubectl -n "$ORIN_NAMESPACE" logs -l app=federation --tail=100 || true
    return 1
  }
}

main() {

  require_root

  parse_args "$@"

  resolve_host_defaults

  require_root_tools

  DETECTED_PLATFORM="$(detect_platform)"

  if [ "$DETECTED_PLATFORM" = "unsupported" ]; then
    echo "Unsupported machine architecture: $(uname -m)"
    exit 1
  fi

  DETECTED_GPU="$(detect_gpu "$DETECTED_PLATFORM")"

  confirm_install

  write_configuration "$DETECTED_PLATFORM" "$DETECTED_GPU"
  configure_registry
  install_k3s_server
  kubectl_wait
  apply_runtime_class_if_needed
  apply_namespace_and_rbac
  apply_cortex_ssh_key_secret
  apply_k3s_token_secret
  apply_configuration_configmap
  label_host_node
  deploy_memory
  wait_for_memory
  deploy_cortex
  wait_for_cortex
  deploy_federation
  wait_for_federation

  echo
  echo "success: Orin deployed"
  echo "cortex endpoint:     http://${HOST_IP}:${CORTEX_NODE_PORT}"
  echo "federation endpoint: http://${HOST_IP}:${FEDERATION_NODE_PORT}"
  echo
  echo "test:"
  echo "  curl -s http://${HOST_IP}:${CORTEX_NODE_PORT}/health"
  echo "  curl -s http://${HOST_IP}:${FEDERATION_NODE_PORT}/health"
}

main "$@"