FROM ubuntu:20.04

ARG GO_VERSION=1.16.5
ARG CAPI_VERSION=v0.3.19
ARG KUBECTL_VERSION=v1.21.1

# Install system APT packages & dependencies
RUN apt-get update && \
    apt-get install -y \
        build-essential curl git libffi-dev libssl-dev \
        python3 python3-pip rsync unzip wget net-tools openssh-client vim

# APT cleanup
RUN apt-get autoclean && \
    apt-get autoremove && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
ADD requirements.txt /tmp
RUN pip3 install -r /tmp/requirements.txt

# Install Golang
ENV GOPATH "/home/prow/go"
ENV PATH "$PATH:/usr/local/go/bin:$GOPATH/bin"
RUN curl -O https://dl.google.com/go/go${GO_VERSION}.linux-amd64.tar.gz && \
    tar -C /usr/local -xzf go${GO_VERSION}.linux-amd64.tar.gz && \
    rm go${GO_VERSION}.linux-amd64.tar.gz && \
    mkdir -p $GOPATH

# Build Kubernetes E2E tests
RUN mkdir -p $GOPATH/src/k8s.io && \
    cd $GOPATH/src/k8s.io && \
    git clone https://github.com/kubernetes/kubernetes -b $KUBECTL_VERSION && \
    cd kubernetes && \
    make WHAT="test/e2e/e2e.test" && \
    make WHAT="vendor/github.com/onsi/ginkgo/ginkgo"

# Build kubetest
RUN mkdir -p $GOPATH/src/github.com/kubernetes && \
    git clone https://github.com/kubernetes/test-infra && \
    cd test-infra && \
    GO111MODULE=on go install ./kubetest

# Install clusterctl
RUN curl -Lo /usr/local/bin/clusterctl "https://github.com/kubernetes-sigs/cluster-api/releases/download/${CAPI_VERSION}/clusterctl-linux-amd64" && \
    chmod +x /usr/local/bin/clusterctl

# Install kubectl
RUN curl -Lo /usr/local/bin/kubectl "https://storage.googleapis.com/kubernetes-release/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    chmod +x /usr/local/bin/kubectl
ENV KUBECTL_PATH "/usr/local/bin/kubectl"

WORKDIR /workspace

ADD cleanup-azure-rgs.py /workspace
ADD entrypoint.sh /workspace
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/workspace/entrypoint.sh"]
