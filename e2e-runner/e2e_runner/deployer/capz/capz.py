import base64
import os
import random
import sh
import yaml

from pathlib import Path
from functools import cached_property
from distutils.util import strtobool
from azure.core import exceptions as azure_exceptions
from azure.identity import ClientSecretCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import models as net_models
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from tenacity import (
    Retrying,
    stop_after_delay,
    wait_exponential,
    retry_if_exception_type,
    retry,
)
from e2e_runner import (
    base,
    constants,
    logger,
    utils
)


class CAPZProvisioner(base.Deployer):

    def __init__(self, opts, container_runtime="docker",
                 flannel_mode=constants.FLANNEL_MODE_OVERLAY,
                 kubernetes_version=constants.DEFAULT_KUBERNETES_VERSION,
                 resource_group_tags={}):
        super(CAPZProvisioner, self).__init__()

        self.e2e_runner_dir = str(Path(__file__).parents[2])
        self.capz_dir = os.path.dirname(__file__)

        self.logging = logger.get_logger(__name__)
        self.kubectl = utils.get_kubectl_bin()
        self.flannel_mode = flannel_mode
        self.container_runtime = container_runtime
        self.bins_built = []
        self.ci_version = kubernetes_version

        self.mgmt_kubeconfig_path = "/tmp/mgmt-kubeconfig.yaml"
        self.capz_kubeconfig_path = "/tmp/capz-kubeconfig.yaml"

        self.cluster_name = opts.cluster_name
        self.cluster_network_subnet = opts.cluster_network_subnet

        self.azure_location = opts.location
        self.master_vm_size = opts.master_vm_size
        self.vnet_cidr_block = opts.vnet_cidr_block
        self.control_plane_subnet_cidr_block = \
            opts.control_plane_subnet_cidr_block
        self.node_subnet_cidr_block = opts.node_subnet_cidr_block
        self.resource_group_tags = resource_group_tags

        self.win_minion_count = opts.win_minion_count
        self.win_minion_size = opts.win_minion_size
        self.win_minion_image_type = opts.win_minion_image_type
        self.win_minion_gallery_image = opts.win_minion_gallery_image
        self.win_minion_image_id = opts.win_minion_image_id

        self.bootstrap_vm_name = "k8s-bootstrap"
        self.bootstrap_vm_nic_name = "k8s-bootstrap-nic"
        self.bootstrap_vm_public_ip_name = "k8s-bootstrap-public-ip"
        self.bootstrap_vm_size = opts.bootstrap_vm_size

        self._set_azure_variables()
        credentials, subscription_id = self._get_azure_credentials()

        self.resource_mgmt_client = ResourceManagementClient(
            credentials, subscription_id)
        self.network_client = NetworkManagementClient(credentials,
                                                      subscription_id)
        self.compute_client = ComputeManagementClient(credentials,
                                                      subscription_id)

        self._setup_infra()
        self._bootstrap_vm  # It will setup the bootstrap Azure VM.
        self._setup_mgmt_kubeconfig()

    @cached_property
    def _bootstrap_vm(self):
        return self._create_bootstrap_vm()

    @cached_property
    def _node_route_table(self):
        return self._create_node_route_table()

    @cached_property
    def master_public_address(self):
        cmd = [
            self.kubectl, "get", "cluster", "--kubeconfig",
            self.mgmt_kubeconfig_path, self.cluster_name, "-o",
            "custom-columns=MASTER_ADDRESS:.spec.controlPlaneEndpoint.host",
            "--no-headers"
        ]
        output, _ = utils.retry_on_error()(utils.run_shell_cmd)(cmd)
        return output.decode().strip()

    @cached_property
    def master_public_port(self):
        cmd = [
            self.kubectl, "get", "cluster", "--kubeconfig",
            self.mgmt_kubeconfig_path, self.cluster_name, "-o",
            "custom-columns=MASTER_PORT:.spec.controlPlaneEndpoint.port",
            "--no-headers"
        ]
        output, _ = utils.retry_on_error()(utils.run_shell_cmd)(cmd)
        return output.decode().strip()

    @cached_property
    def linux_private_addresses(self):
        return self._get_agents_private_addresses("linux")

    @cached_property
    def windows_private_addresses(self):
        return self._get_agents_private_addresses("windows")

    @property
    def remote_go_path(self):
        return "~/go"

    @property
    def remote_k8s_path(self):
        return os.path.join(self.remote_go_path, "src/k8s.io/kubernetes")

    @property
    def remote_containerd_path(self):
        return os.path.join(self.remote_go_path,
                            "src", "github.com", "containerd", "containerd")

    @property
    def remote_artifacts_dir(self):
        return "~/www"

    @property
    def remote_sdn_path(self):
        return os.path.join(self.remote_go_path,
                            "src", "github.com",
                            "Microsoft", "windows-container-networking")

    @property
    def remote_test_infra_path(self):
        return os.path.join(self.remote_go_path,
                            "src", "github.com", "kubernetes", "test-infra")

    @property
    def remote_containerd_shim_path(self):
        return os.path.join(self.remote_go_path,
                            "src", "github.com", "Microsoft", "hcsshim")

    @property
    def bootstrap_vm_private_ip(self):
        return self._bootstrap_vm['private_ip']

    @property
    def bootstrap_vm_public_ip(self):
        return self._bootstrap_vm['public_ip']

    def up(self):
        self._setup_capz_components()
        self._create_capz_cluster()
        self._wait_for_control_plane()
        self._setup_capz_kubeconfig()

    @utils.retry_on_error()
    def down(self, wait=True):
        self.logging.info("Deleting Azure resource group")
        client = self.resource_mgmt_client
        try:
            delete_async_operation = client.resource_groups.begin_delete(
                self.cluster_name)
            if wait:
                delete_async_operation.wait()
        except azure_exceptions.ResourceNotFoundError as e:
            cloud_error_data = e.error
            if cloud_error_data.error == "ResourceGroupNotFound":
                self.logging.warning("Resource group %s does not exist",
                                     self.cluster_name)
            else:
                raise e

    def reclaim(self):
        self._setup_capz_kubeconfig()

    def add_win_agents_kubelet_args(self, kubelet_args):
        extra_kubelet_args = ' '.join(kubelet_args)
        self.logging.info(
            "Adding the following extra args to the Windows agents "
            "kubelets: %s", extra_kubelet_args)
        kubeadm_flags_env_file = '/var/lib/kubelet/kubeadm-flags.env'
        local_kubeadm_flags_env_file = '/tmp/kubeadm-flags.env'
        for win_address in self.windows_private_addresses:
            self.download_from_k8s_node(
                kubeadm_flags_env_file,
                local_kubeadm_flags_env_file,
                win_address)
            with open(local_kubeadm_flags_env_file, 'r') as f:
                flags = f.read().strip('KUBELET_KUBEADM_ARGS="\n')
            flags += ' {}'.format(extra_kubelet_args)
            kubeadm_flags_env = 'KUBELET_KUBEADM_ARGS="{}"\n'.format(flags)
            with open(local_kubeadm_flags_env_file, 'w') as f:
                f.write(kubeadm_flags_env)
            self.upload_to_k8s_node(
                local_kubeadm_flags_env_file,
                kubeadm_flags_env_file,
                win_address)
            self.run_cmd_on_k8s_node('nssm restart kubelet', win_address)

    def setup_ssh_config(self):
        ssh_dir = os.path.join(os.environ["HOME"], ".ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        ssh_config = [
            "Host %s" % self.master_public_address,
            "HostName %s" % self.master_public_address,
            "User capi",
            "StrictHostKeyChecking no",
            "UserKnownHostsFile /dev/null",
            "IdentityFile %s" % os.environ["SSH_KEY"],
            ""
        ]
        k8s_master_ssh_host = self.master_public_address
        agents_private_addresses = self.windows_private_addresses + \
            self.linux_private_addresses
        for address in agents_private_addresses:
            ssh_config += [
                "Host %s" % address,
                "HostName %s" % address,
                "User capi",
                "ProxyCommand ssh -q %s -W %%h:%%p" % k8s_master_ssh_host,
                "StrictHostKeyChecking no",
                "UserKnownHostsFile /dev/null",
                "IdentityFile %s" % os.environ["SSH_KEY"],
                ""
            ]
        ssh_config_file = os.path.join(ssh_dir, "config")
        with open(ssh_config_file, "w") as f:
            f.write("\n".join(ssh_config))

    def assert_nodes_successful_provision(self):
        nodes_addresses = \
            self.windows_private_addresses + self.linux_private_addresses
        for address in nodes_addresses:
            self.logging.info(
                "Checking if node %s provisioned successfully", address)
            is_successful, _ = self.run_cmd_on_k8s_node(
                "cat /tmp/kubeadm-success.txt", address)
            if not strtobool(is_successful.decode().strip()):
                msg = "Node {} didn't provision successfully".format(address)
                self.logging.error(msg)
                raise Exception(msg)

    def enable_ip_forwarding(self):
        self.logging.info("Enabling IP forwarding for the cluster VMs")
        vm_nics = utils.retry_on_error()(
            self.network_client.network_interfaces.list)(self.cluster_name)
        for nic in vm_nics:
            if nic.name == self.bootstrap_vm_nic_name:
                continue
            if nic.enable_ip_forwarding:
                self.logging.info(
                    "IP forwarding is already enabled on nic %s", nic.name)
                continue
            self.logging.info("Enabling IP forwarding on nic %s", nic.name)
            nic_parameters = nic.as_dict()
            nic_parameters["enable_ip_forwarding"] = True
            utils.retry_on_error()(
                self.network_client.network_interfaces.begin_create_or_update)(
                    self.cluster_name,
                    nic.name,
                    nic_parameters).result()

    def wait_for_agents(self, timeout=3600):
        self.logging.info("Waiting up to %.2f minutes for CAPZ machines",
                          timeout / 60.0)
        for attempt in Retrying(
                stop=stop_after_delay(timeout),
                wait=wait_exponential(max=30),
                retry=retry_if_exception_type(AssertionError),
                reraise=True):
            with attempt:
                ready_nodes = []
                machines = self._get_mgmt_capz_machines_names()
                for machine in machines:
                    is_control_plane = machine.startswith(
                        '{}-control-plane'.format(self.cluster_name))
                    if is_control_plane:
                        continue
                    try:
                        phase = self._get_mgmt_capz_machine_phase(machine)
                    except Exception:
                        continue
                    if phase != "Running":
                        continue
                    node_name = self._get_mgmt_capz_machine_node(machine)
                    if node_name not in self._get_capz_nodes():
                        continue
                    if self.container_runtime == "docker":
                        # Kubernetes Docker agents don't have a default CNI
                        # configured. Therefore the agents will not report
                        # ready until a CNI is initialized.
                        # Therefore, at least we verify that the ready
                        # condition is reported by the agents.
                        r = self._get_capz_node_ready_condition(node_name)
                        if not r:
                            continue
                        if r.get('reason') == 'KubeletNotReady':
                            ready_nodes.append(node_name)
                    else:
                        if self._is_k8s_node_ready(node_name):
                            ready_nodes.append(node_name)
                assert len(ready_nodes) == self.win_minion_count
                assert set(ready_nodes) == set(self._get_capz_nodes())
        self.logging.info("All CAPZ agents are ready")

    @utils.retry_on_error()
    def upload_to_bootstrap_vm(self, local_path, remote_path="www/"):
        utils.rsync_upload(
            local_path=local_path, remote_path=remote_path,
            ssh_user="capi", ssh_address=self.bootstrap_vm_public_ip,
            ssh_key_path=os.environ["SSH_KEY"])

    @utils.retry_on_error()
    def download_from_bootstrap_vm(self, remote_path, local_path):
        ssh_cmd = ("ssh -q -i {} "
                   "-o StrictHostKeyChecking=no "
                   "-o UserKnownHostsFile=/dev/null".format(
                       os.environ["SSH_KEY"]))
        utils.run_shell_cmd([
            "rsync", "-a", "-e", '"{}"'.format(ssh_cmd), "--delete",
            "capi@{}:{}".format(self.bootstrap_vm_public_ip, remote_path),
            local_path])

    @utils.retry_on_error()
    def run_cmd_on_bootstrap_vm(self, cmd, timeout=3600, cwd="~",
                                return_result=False):
        return utils.run_remote_ssh_cmd(
            cmd=cmd, ssh_user="capi", ssh_address=self.bootstrap_vm_public_ip,
            ssh_key_path=os.environ["SSH_KEY"], cwd=cwd, timeout=timeout,
            return_result=return_result)

    def remote_clone_git_repo(self, repo_url, branch_name, remote_dir):
        clone_cmd = ("test -e {0} || "
                     "git clone --single-branch {1} --branch {2} {0}").format(
                         remote_dir, repo_url, branch_name)
        self.run_cmd_on_bootstrap_vm([clone_cmd])

    @utils.retry_on_error()
    def run_cmd_on_k8s_node(self, cmd, node_address):
        cmd = ["ssh", node_address, "'{}'".format(cmd)]
        return utils.run_shell_cmd(cmd, timeout=600)

    def run_async_cmd_on_k8s_node(self, cmd, node_address):
        return utils.run_async_shell_cmd(
            sh.ssh, [node_address, cmd])

    @utils.retry_on_error()
    def download_from_k8s_node(self, remote_path, local_path, node_address):
        cmd = ["scp", "-r",
               "{}:{}".format(node_address, remote_path), local_path]
        utils.run_shell_cmd(cmd, timeout=600)

    @utils.retry_on_error()
    def upload_to_k8s_node(self, local_path, remote_path, node_address):
        cmd = ["scp", "-r",
               local_path, "{}:{}".format(node_address, remote_path)]
        utils.run_shell_cmd(cmd, timeout=600)

    def check_k8s_node_connection(self, node_address):
        cmd = ["ssh", self.master_public_address,
               "'nc -w 5 -z %s 22'" % node_address]
        _, _, ret = utils.run_cmd(cmd, shell=True, sensitive=True, timeout=60)
        if ret == 0:
            return True
        return False

    def cleanup_bootstrap_vm(self):
        self.logging.info("Cleaning up the bootstrap VM")
        self.logging.info("Deleting bootstrap VM")
        utils.retry_on_error()(
            self.compute_client.virtual_machines.begin_delete)(
                self.cluster_name, self.bootstrap_vm_name).wait()
        self.logging.info("Deleting bootstrap VM NIC")
        utils.retry_on_error()(
            self.network_client.network_interfaces.begin_delete)(
                self.cluster_name, self.bootstrap_vm_nic_name).wait()
        self.logging.info("Deleting bootstrap VM public IP")
        utils.retry_on_error()(
            self.network_client.public_ip_addresses.begin_delete)(
                self.cluster_name, self.bootstrap_vm_public_ip_name).wait()

    def connect_agents_to_controlplane_subnet(self):
        self.logging.info("Connecting agents VMs to the control-plane subnet")
        control_plane_subnet = utils.retry_on_error()(
            self.network_client.subnets.get)(
                self.cluster_name,
                "{}-vnet".format(self.cluster_name),
                "{}-controlplane-subnet".format(self.cluster_name))
        subnet_id = control_plane_subnet.id
        for vm in self._get_agents_vms():
            self.logging.info("Connecting VM {}".format(vm.name))
            nic_id = vm.network_profile.network_interfaces[0].id
            vm_nic = self._get_vm_nic(nic_id)
            nic_address = vm_nic.ip_configurations[0].private_ip_address
            route = self._get_vm_route(nic_address)
            self.logging.info("Shutting down VM")
            utils.retry_on_error()(
                self.compute_client.virtual_machines.begin_deallocate)(
                    self.cluster_name, vm.name).wait()
            self.logging.info("Updating VM NIC subnet")
            nic_parameters = vm_nic.as_dict()
            nic_model = net_models.NetworkInterface(**nic_parameters)
            nic_model.ip_configurations[0]['subnet']['id'] = subnet_id
            utils.retry_on_error()(
                self.network_client.network_interfaces.begin_create_or_update)(
                    self.cluster_name, vm_nic.name, nic_model).wait()
            self.logging.info("Starting VM")
            utils.retry_on_error()(
                self.compute_client.virtual_machines.begin_start)(
                    self.cluster_name, vm.name).wait()
            self.logging.info("Updating the node routetable")
            route_params = route.as_dict()
            vm_nic = self._get_vm_nic(nic_id)  # Refresh NIC info
            nic_address = vm_nic.ip_configurations[0].private_ip_address
            route_params["next_hop_ip_address"] = nic_address
            utils.retry_on_error()(
                self.network_client.routes.begin_create_or_update)(
                    self.cluster_name,
                    "{}-node-routetable".format(self.cluster_name),
                    route.name,
                    route_params).wait()
            self.logging.info(
                "Waiting until VM address is refreshed in the CAPZ cluster")
            for attempt in Retrying(
                    stop=stop_after_delay(10 * 60),
                    wait=wait_exponential(max=30),
                    reraise=True):
                with attempt:
                    addresses = self._get_agents_private_addresses("windows")
                    assert nic_address in addresses

    def _get_agents_private_addresses(self, operating_system):
        cmd = [
            self.kubectl, "get", "nodes", "--kubeconfig",
            self.capz_kubeconfig_path, "-o", "yaml"
        ]
        output, _ = utils.retry_on_error()(
            utils.run_shell_cmd)(cmd, sensitive=True)
        addresses = []
        nodes = yaml.safe_load(output)
        for node in nodes['items']:
            node_os = node['status']['nodeInfo']['operatingSystem']
            if node_os != operating_system:
                continue
            try:
                node_addresses = [
                    n['address'] for n in node['status']['addresses']
                    if n['type'] == 'InternalIP'
                ]
            except Exception as ex:
                self.logging.warning(
                    "Cannot find private address for node %s. Exception "
                    "details: %s. Skipping", node["metadata"]["name"], ex)
                continue
            # pick the first node internal address
            addresses.append(node_addresses[0])
        return addresses

    def _parse_win_minion_image_gallery(self):
        split = self.win_minion_gallery_image.split(":")
        if len(split) != 4:
            err_msg = ("Incorrect format for the --win-minion-image-gallery "
                       "parameter")
            self.logging.error(err_msg)
            raise Exception(err_msg)
        return {
            "resource_group": split[0],
            "gallery_name": split[1],
            "image_definition": split[2],
            "image_version": split[3]
        }

    def _get_azure_credentials(self):
        credentials = ClientSecretCredential(
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
            tenant_id=os.environ["AZURE_TENANT_ID"])
        subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
        return credentials, subscription_id

    @utils.retry_on_error()
    def _get_agents_vms(self):
        for vm in self.compute_client.virtual_machines.list(self.cluster_name):
            if vm.storage_profile.os_disk.os_type == 'Windows':
                yield vm

    @utils.retry_on_error()
    def _get_vm_nic(self, nic_id):
        nics = self.network_client.network_interfaces.list(self.cluster_name)
        for nic in nics:
            if nic.id == nic_id:
                return nic
        raise Exception("The VM NIC was not found: {}".format(nic_id))

    @retry(stop=stop_after_delay(900),
           wait=wait_exponential(max=30),
           reraise=True)
    def _get_vm_route(self, next_hop_ip_address):
        routes = self.network_client.routes.list(
            self.cluster_name,
            "{}-node-routetable".format(self.cluster_name))
        for route in routes:
            if route.next_hop_ip_address == next_hop_ip_address:
                return route
        raise Exception("The VM route with next_hop {} was not found".format(
            next_hop_ip_address))

    def _wait_for_bootstrap_vm(self, timeout=900):
        self.logging.info("Waiting up to %.2f minutes for VM %s to provision",
                          timeout / 60.0, self.bootstrap_vm_name)
        valid_vm_states = ["Creating", "Updating", "Succeeded"]
        for attempt in Retrying(
                stop=stop_after_delay(timeout),
                wait=wait_exponential(max=30),
                retry=retry_if_exception_type(AssertionError),
                reraise=True):
            with attempt:
                vm = utils.retry_on_error()(
                    self.compute_client.virtual_machines.get)(
                        self.cluster_name,
                        self.bootstrap_vm_name)
                if vm.provisioning_state not in valid_vm_states:
                    err_msg = 'VM "{}" entered invalid state: "{}"'.format(
                        self.bootstrap_vm_name, vm.provisioning_state)
                    self.logging.error(err_msg)
                    raise azure_exceptions.AzureError(err_msg)
                assert vm.provisioning_state == "Succeeded"
        return vm

    def _create_bootstrap_vm_public_ip(self):
        self.logging.info("Creating bootstrap VM public IP")
        public_ip_parameters = {
            "location": self.azure_location,
            "public_ip_address_version": "IPV4"
        }
        return utils.retry_on_error()(
            self.network_client.public_ip_addresses.begin_create_or_update)(
                self.cluster_name,
                self.bootstrap_vm_public_ip_name,
                public_ip_parameters).result()

    def _create_bootstrap_vm_nic(self):
        self.logging.info("Creating bootstrap VM NIC")
        public_ip = self._create_bootstrap_vm_public_ip()
        control_plane_subnet = utils.retry_on_error()(
            self.network_client.subnets.get)(
                self.cluster_name,
                "%s-vnet" % self.cluster_name,
                "%s-controlplane-subnet" % self.cluster_name)
        nic_parameters = {
            "location": self.azure_location,
            "ip_configurations": [{
                "name": "%s-ipconfig" % self.bootstrap_vm_nic_name,
                "subnet": {
                    "id": control_plane_subnet.id
                },
                "public_ip_address": {
                    "id": public_ip.id
                }
            }]
        }
        return utils.retry_on_error()(
            self.network_client.network_interfaces.begin_create_or_update)(
                self.cluster_name,
                self.bootstrap_vm_nic_name,
                nic_parameters).result()

    @utils.retry_on_error()
    def _init_bootstrap_vm(self, bootstrap_vm_address):
        self.logging.info("Initializing the bootstrap VM")
        cmd = ["mkdir -p www",
               "sudo addgroup --system docker",
               "sudo usermod -aG docker capi"]
        utils.run_remote_ssh_cmd(
            cmd=cmd, ssh_user="capi", ssh_address=bootstrap_vm_address,
            ssh_key_path=os.environ["SSH_KEY"])
        utils.rsync_upload(
            local_path=os.path.join(self.e2e_runner_dir, "scripts"),
            remote_path="www/",
            ssh_user="capi", ssh_address=bootstrap_vm_address,
            ssh_key_path=os.environ["SSH_KEY"])
        cmd = ["bash ./www/scripts/init-bootstrap-vm.sh"]
        utils.run_remote_ssh_cmd(
            cmd=cmd, ssh_user="capi", ssh_address=bootstrap_vm_address,
            ssh_key_path=os.environ["SSH_KEY"], timeout=(60 * 15))

    def _create_bootstrap_azure_vm(self):
        self.logging.info("Setting up the bootstrap Azure VM")
        vm_nic = self._create_bootstrap_vm_nic()
        vm_parameters = {
            "location": self.azure_location,
            "os_profile": {
                "computer_name": self.bootstrap_vm_name,
                "admin_username": "capi",
                "linux_configuration": {
                    "disable_password_authentication": True,
                    "ssh": {
                        "public_keys": [{
                            "key_data": os.environ["AZURE_SSH_PUBLIC_KEY"],
                            "path": "/home/capi/.ssh/authorized_keys"
                        }]
                    }
                }
            },
            "hardware_profile": {
                "vm_size": self.bootstrap_vm_size
            },
            "storage_profile": {
                "image_reference": {
                    "publisher": "Canonical",
                    "offer": "0001-com-ubuntu-server-focal",
                    "sku": "20_04-lts-gen2",
                    "version": "latest"
                },
            },
            "network_profile": {
                "network_interfaces": [{
                    "id": vm_nic.id
                }]
            }
        }
        self.logging.info("Creating bootstrap VM")
        vm = utils.retry_on_error()(
            self.compute_client.virtual_machines.begin_create_or_update)(
                self.cluster_name,
                self.bootstrap_vm_name,
                vm_parameters).result()
        vm = self._wait_for_bootstrap_vm()
        ip_config = utils.retry_on_error()(
            self.network_client.network_interfaces.get)(
                self.cluster_name, vm_nic.name).ip_configurations[0]
        bootstrap_vm_private_ip = ip_config.private_ip_address
        public_ip = utils.retry_on_error()(
            self.network_client.public_ip_addresses.get)(
                self.cluster_name, self.bootstrap_vm_public_ip_name)
        bootstrap_vm_public_ip = public_ip.ip_address
        self.logging.info("Waiting for bootstrap VM SSH port to be reachable")
        utils.wait_for_port_connectivity(bootstrap_vm_public_ip, 22)
        self.logging.info("Finished setting up the bootstrap VM")
        return {
            'private_ip': bootstrap_vm_private_ip,
            'public_ip': bootstrap_vm_public_ip,
            'vm': vm,
        }

    def _create_resource_group(self):
        self.logging.info("Creating Azure resource group")
        resource_group_params = {
            'location': self.azure_location,
            'tags': self.resource_group_tags,
        }
        self.resource_mgmt_client.resource_groups.create_or_update(
            self.cluster_name,
            resource_group_params)
        for attempt in Retrying(
                stop=stop_after_delay(600),
                wait=wait_exponential(max=30),
                retry=retry_if_exception_type(AssertionError),
                reraise=True):
            with attempt:
                rg = utils.retry_on_error()(
                    self.resource_mgmt_client.resource_groups.get)(
                        self.cluster_name)
                assert rg.properties.provisioning_state == "Succeeded"

    @utils.retry_on_error()
    def _create_vnet(self):
        self.logging.info("Creating Azure vNET")
        vnet_params = {
            "location": self.azure_location,
            "address_space": {
                "address_prefixes": [self.vnet_cidr_block]
            }
        }
        return self.network_client.virtual_networks.begin_create_or_update(
            self.cluster_name,
            "{}-vnet".format(self.cluster_name),
            vnet_params).result()

    @utils.retry_on_error()
    def _create_node_route_table(self):
        self.logging.info("Creating Azure node route table")
        route_table_params = {
            "location": self.azure_location,
        }
        return self.network_client.route_tables.begin_create_or_update(
            self.cluster_name,
            "{}-node-routetable".format(self.cluster_name),
            route_table_params).result()

    @utils.retry_on_error()
    def _create_control_plane_secgroup(self):
        secgroup_rules = [
            net_models.SecurityRule(
                protocol="Tcp",
                priority="1000",
                source_port_range="*",
                source_address_prefix="0.0.0.0/0",
                destination_port_range="22",
                destination_address_prefix="0.0.0.0/0",
                destination_address_prefixes=[],
                destination_application_security_groups=[],
                access=net_models.SecurityRuleAccess.allow,
                direction=net_models.SecurityRuleDirection.inbound,
                name="Allow_SSH"),
            net_models.SecurityRule(
                protocol="Tcp",
                priority="1001",
                source_port_range="*",
                source_address_prefix="0.0.0.0/0",
                destination_port_range="6443",
                destination_address_prefix="0.0.0.0/0",
                destination_address_prefixes=[],
                destination_application_security_groups=[],
                access=net_models.SecurityRuleAccess.allow,
                direction=net_models.SecurityRuleDirection.inbound,
                name="Allow_K8s_API")
        ]
        secgroup_name = "{}-controlplane-nsg".format(self.cluster_name)
        secgroup_params = net_models.NetworkSecurityGroup(
            name=secgroup_name,
            location=self.azure_location,
            security_rules=secgroup_rules)
        nsg_client = self.network_client.network_security_groups
        return nsg_client.begin_create_or_update(
            self.cluster_name, secgroup_name, secgroup_params).result()

    def _create_control_plane_subnet(self):
        self.logging.info("Creating Azure vNET control plane subnet")
        nsg = self._create_control_plane_secgroup()
        route_table = self._node_route_table
        subnet_params = {
            "address_prefix": self.control_plane_subnet_cidr_block,
            "network_security_group": {
                "id": nsg.id
            },
            "route_table": {
                "id": route_table.id
            },
        }
        return utils.retry_on_error()(
            self.network_client.subnets.begin_create_or_update)(
                self.cluster_name,
                "{}-vnet".format(self.cluster_name),
                "{}-controlplane-subnet".format(self.cluster_name),
                subnet_params).result()

    @utils.retry_on_error()
    def _create_node_secgroup(self):
        secgroup_name = "{}-node-nsg".format(self.cluster_name)
        secgroup_params = net_models.NetworkSecurityGroup(
            name=secgroup_name,
            location=self.azure_location)
        nsg_client = self.network_client.network_security_groups
        return nsg_client.begin_create_or_update(
            self.cluster_name, secgroup_name, secgroup_params).result()

    def _create_node_subnet(self):
        self.logging.info("Creating Azure vNET node subnet")
        nsg = self._create_node_secgroup()
        route_table = self._node_route_table
        subnet_params = {
            "address_prefix": self.node_subnet_cidr_block,
            "network_security_group": {
                "id": nsg.id
            },
            "route_table": {
                "id": route_table.id
            },
        }
        return utils.retry_on_error()(
            self.network_client.subnets.begin_create_or_update)(
                self.cluster_name,
                "{}-vnet".format(self.cluster_name),
                "{}-node-subnet".format(self.cluster_name),
                subnet_params).result()

    @utils.retry_on_error()
    def _create_bootstrap_vm(self):
        try:
            bootstrap_vm = self._create_bootstrap_azure_vm()
            self._init_bootstrap_vm(bootstrap_vm['public_ip'])
        except Exception as ex:
            self.cleanup_bootstrap_vm()
            raise ex
        return bootstrap_vm

    @utils.retry_on_error()
    def _setup_infra(self):
        self.logging.info("Setting up the testing infra")
        try:
            self._create_resource_group()
            self._create_vnet()
            self._create_control_plane_subnet()
            self._create_node_subnet()
        except Exception as ex:
            self.down()  # Deletes the resource group
            raise ex

    @utils.retry_on_error()
    def _get_mgmt_capz_machines_names(self):
        cmd = [
            self.kubectl, "get", "machine",
            "--kubeconfig", self.mgmt_kubeconfig_path,
            "--output=custom-columns=NAME:.metadata.name",
            "--no-headers"
        ]
        output, _ = utils.run_shell_cmd(cmd, sensitive=True)
        return output.decode().strip().split('\n')

    @utils.retry_on_error()
    def _get_capz_nodes(self):
        cmd = [
            self.kubectl, "get", "nodes",
            "--kubeconfig", self.capz_kubeconfig_path,
            "-l", "!node-role.kubernetes.io/control-plane",
            "--output=custom-columns=NAME:.metadata.name",
            "--no-headers"
        ]
        output, _ = utils.run_shell_cmd(cmd, sensitive=True)
        return output.decode().strip().split('\n')

    @utils.retry_on_error()
    def _get_mgmt_capz_machine_phase(self, machine_name):
        cmd = [
            self.kubectl, "get", "machine",
            "--kubeconfig", self.mgmt_kubeconfig_path,
            "--output=custom-columns=READY:.status.phase",
            "--no-headers", machine_name
        ]
        output, _ = utils.run_shell_cmd(cmd, sensitive=True)
        return output.decode().strip()

    @utils.retry_on_error()
    def _get_mgmt_capz_machine_node(self, machine_name):
        cmd = [
            self.kubectl, "get", "machine",
            "--kubeconfig", self.mgmt_kubeconfig_path,
            "--output=custom-columns=NODE_NAME:.status.nodeRef.name",
            "--no-headers", machine_name
        ]
        output, _ = utils.run_shell_cmd(cmd, sensitive=True)
        return output.decode().strip()

    @utils.retry_on_error()
    def _get_capz_node_status(self, node_name):
        cmd = [
            self.kubectl, "get", "node", "--output", "yaml",
            "--kubeconfig", self.capz_kubeconfig_path,
            node_name
        ]
        output, _ = utils.run_shell_cmd(cmd, sensitive=True)
        node = yaml.safe_load(output.decode())
        if "status" not in node:
            return None
        return node["status"]

    def _get_capz_node_status_conditions(self, node_name,
                                         condition_type="Ready"):
        node_status = self._get_capz_node_status(node_name)
        if not node_status:
            return []
        return [
            c for c in node_status["conditions"] if c["type"] == condition_type
        ]

    def _get_capz_node_ready_condition(self, node_name):
        ready_conditions = self._get_capz_node_status_conditions(
            node_name, "Ready")
        if len(ready_conditions) == 0:
            return None
        return ready_conditions[0]

    def _is_k8s_node_ready(self, node_name=None):
        if not node_name:
            self.logging.warning("Empty node_name parameter")
            return False
        ready_condition = self._get_capz_node_ready_condition(node_name)
        if not ready_condition:
            self.logging.info("Node %s didn't report ready condition yet",
                              node_name)
            return False
        try:
            is_ready = strtobool(ready_condition["status"])
        except ValueError:
            is_ready = False
        if not is_ready:
            self.logging.info("Node %s is not ready yet", node_name)
            return False
        return True

    def _create_capz_cluster(self):
        bootstrap_vm_address = "{}:8081".format(self.bootstrap_vm_private_ip)
        context = {
            "cluster_name": self.cluster_name,
            "cluster_network_subnet": self.cluster_network_subnet,
            "azure_location": self.azure_location,
            "azure_subscription_id": os.environ["AZURE_SUBSCRIPTION_ID"],
            "azure_tenant_id": os.environ["AZURE_TENANT_ID"],
            "azure_client_id": os.environ["AZURE_CLIENT_ID"],
            "azure_client_secret": os.environ["AZURE_CLIENT_SECRET"],
            "azure_ssh_public_key": os.environ["AZURE_SSH_PUBLIC_KEY"],
            "azure_ssh_public_key_b64": os.environ["AZURE_SSH_PUBLIC_KEY_B64"],
            "master_vm_size": self.master_vm_size,
            "win_minion_count": self.win_minion_count,
            "win_minion_size": self.win_minion_size,
            "win_minion_image_type": self.win_minion_image_type,
            "bootstrap_vm_address": bootstrap_vm_address,
            "ci_version": self.ci_version,
            "flannel_mode": self.flannel_mode,
            "container_runtime": self.container_runtime,
            "k8s_bins": "k8sbins" in self.bins_built,
            "sdn_cni_bins": "sdncnibins" in self.bins_built,
            "containerd_bins": "containerdbins" in self.bins_built,
            "containerd_shim_bins": "containerdshim" in self.bins_built,
        }
        if self.win_minion_image_type == constants.SHARED_IMAGE_GALLERY_TYPE:
            parsed = self._parse_win_minion_image_gallery()
            context["win_minion_image_rg"] = parsed["resource_group"]
            context["win_minion_image_gallery"] = parsed["gallery_name"]
            context["win_minion_image_definition"] = parsed["image_definition"]
            context["win_minion_image_version"] = parsed["image_version"]
        elif self.win_minion_image_type == constants.MANAGED_IMAGE_TYPE:
            context["win_minion_image_id"] = self.win_minion_image_id
        self.logging.info("Create CAPZ cluster")
        output_file = "/tmp/capz-cluster.yaml"
        utils.render_template(
            "cluster.yaml.j2", output_file, context, self.capz_dir)
        utils.retry_on_error()(utils.run_shell_cmd)([
            self.kubectl, "apply", "--kubeconfig",
            self.mgmt_kubeconfig_path, "-f", output_file
        ])

    def _setup_mgmt_kubeconfig(self):
        self.logging.info("Setting up the management cluster kubeconfig")
        self.download_from_bootstrap_vm(
            ".kube/config", self.mgmt_kubeconfig_path)
        with open(self.mgmt_kubeconfig_path, 'r') as f:
            kubecfg = yaml.safe_load(f.read())
        k8s_cluster = kubecfg["clusters"][0]["cluster"]
        k8s_cluster["server"] = "https://{}:6443".format(
            self.bootstrap_vm_public_ip)
        with open(self.mgmt_kubeconfig_path, 'w') as f:
            f.write(yaml.safe_dump(kubecfg))

    @utils.retry_on_error()
    def _setup_capz_kubeconfig(self):
        self.logging.info("Setting up CAPZ kubeconfig")
        cmd = [
            self.kubectl, "get", "--kubeconfig", self.mgmt_kubeconfig_path,
            "secret/%s-kubeconfig" % self.cluster_name,
            "--output=custom-columns=KUBECONFIG_B64:.data.value",
            "--no-headers"
        ]
        output, _ = utils.run_shell_cmd(cmd)
        with open(self.capz_kubeconfig_path, 'w') as f:
            f.write(base64.b64decode(output).decode())

    def _wait_for_control_plane(self, timeout=2700):
        self.logging.info(
            "Waiting up to %.2f minutes for the control-plane to be ready.",
            timeout / 60.0)
        machines_list_cmd = [
            self.kubectl, "get", "machine",
            "--kubeconfig", self.mgmt_kubeconfig_path,
            "--output=custom-columns=NAME:.metadata.name",
            "--no-headers"
        ]
        control_plane_name_prefix = "{}-control-plane".format(
            self.cluster_name)
        for attempt in Retrying(
                stop=stop_after_delay(timeout),
                wait=wait_exponential(max=30),
                retry=retry_if_exception_type(AssertionError),
                reraise=True):
            with attempt:
                output, _ = utils.retry_on_error()(
                    utils.run_shell_cmd)(machines_list_cmd, sensitive=True)
                machines = output.decode().strip().split('\n')
                control_plane_machines = [
                    m for m in machines
                    if m.startswith(control_plane_name_prefix)
                ]
                assert len(control_plane_machines) > 0
                control_plane_ready = True
                for control_plane_machine in control_plane_machines:
                    try:
                        status_phase = self._get_mgmt_capz_machine_phase(
                            control_plane_machine)
                    except Exception:
                        control_plane_ready = False
                        break
                    if status_phase != "Running":
                        control_plane_ready = False
                        break
                assert control_plane_ready
        self.logging.info("Control-plane is ready")

    @utils.retry_on_error(max_attempts=10)
    def _setup_capz_components(self):
        self.logging.info("Setup the Azure Cluster API components")
        utils.run_shell_cmd([
            "clusterctl", "init",
            "--kubeconfig", self.mgmt_kubeconfig_path,
            "--core", ("cluster-api:%s" % constants.CAPI_VERSION),
            "--bootstrap", ("kubeadm:%s" % constants.CAPI_VERSION),
            "--control-plane", ("kubeadm:%s" % constants.CAPI_VERSION),
            "--infrastructure", ("azure:%s" % constants.CAPZ_PROVIDER_VERSION)
        ])
        self.logging.info("Wait for the deployments to be available")
        utils.run_shell_cmd([
            self.kubectl, "wait", "--kubeconfig",
            self.mgmt_kubeconfig_path, "--for=condition=Available",
            "--timeout", "5m", "deployments", "--all", "--all-namespaces"
        ])

    def _set_azure_variables(self):
        # Define the required env variables list
        required_env_vars = [
            "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET", "AZURE_SSH_PUBLIC_KEY"
        ]
        # Check for alternate env variables names set in the CI if
        # the expected ones are empty
        if (not os.environ.get("AZURE_SUBSCRIPTION_ID")
                and os.environ.get("AZURE_SUB_ID")):
            os.environ["AZURE_SUBSCRIPTION_ID"] = os.environ.get(
                "AZURE_SUB_ID")
        if (not os.environ.get("AZURE_SSH_PUBLIC_KEY")
                and os.environ.get("SSH_KEY_PUB")):
            with open(os.environ.get("SSH_KEY_PUB").strip()) as f:
                os.environ["AZURE_SSH_PUBLIC_KEY"] = f.read().strip()
        # Check if the required env variables are set, and set their
        # base64 variants
        for env_var in required_env_vars:
            if not os.environ.get(env_var):
                raise Exception("Env variable %s is not set" % env_var)
            os.environ[env_var] = os.environ.get(env_var).strip()
            b64_env_var = "%s_B64" % env_var
            os.environ[b64_env_var] = base64.b64encode(
                os.environ.get(env_var).encode()).decode()
        # Set Azure location if it's not set already
        if not self.azure_location:
            self.azure_location = random.choice(constants.AZURE_LOCATIONS)
