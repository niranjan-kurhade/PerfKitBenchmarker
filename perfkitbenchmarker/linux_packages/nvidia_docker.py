# Copyright 2019 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Module containing NVIDIA Container Runtime for Docker installation.

Installation: https://github.com/NVIDIA/nvidia-docker
"""
from absl import flags
_VERSION = flags.DEFINE_string('nvidia_docker_version', None,
                               'The version of nvidia docker to install.')


def CheckNvidiaDockerExists(vm):
  resp, _ = vm.RemoteHostCommand(
      'command -v nvidia-docker', ignore_failure=True
  )
  return bool(resp.rstrip())


def AptInstall(vm):
  """Installs the nvidia-docker package on the VM."""
  if CheckNvidiaDockerExists(vm):
    return
  vm.Install('docker')
  vm.RemoteCommand('curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey '
                   '| sudo apt-key add -')
  vm.RemoteCommand('curl -s -L https://nvidia.github.io/nvidia-docker/'
                   '$(. /etc/os-release;echo $ID$VERSION_ID)'
                   '/nvidia-docker.list | sudo tee '
                   '/etc/apt/sources.list.d/nvidia-container-toolkit.list')
  vm.RemoteCommand('sudo apt-get update')
  version = f'={_VERSION.value}' if _VERSION.value else ''
  vm.InstallPackages(f'nvidia-docker2{version}')
  # Reload the Docker daemon configuration
  vm.RemoteCommand('sudo pkill -SIGHUP dockerd')


def YumInstall(vm):
  """Installs the nvidia-docker package on the VM."""
  if CheckNvidiaDockerExists(vm):
    return
  vm.Install('docker')
  vm.RemoteCommand('curl -s -L https://nvidia.github.io/'
                   'nvidia-container-runtime/'
                   '$(. /etc/os-release;echo $ID$VERSION_ID)/'
                   'nvidia-container-runtime.repo | sudo tee /etc/yum.repos.d/'
                   'nvidia-container-runtime.repo')
  vm.RemoteCommand('sudo tee /etc/yum.repos.d/nvidia-container-runtime.repo')
  vm.InstallPackages('nvidia-container-runtime-hook')
