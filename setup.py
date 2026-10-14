"""Mark the vendored Linux x86-64 ELF as platform-specific wheel content."""

from setuptools import setup
from wheel.bdist_wheel import bdist_wheel
import platform as host_platform
import sys
import sysconfig


class PlatformWheel(bdist_wheel):
    def get_tag(self):
        if sys.platform != "linux" or host_platform.machine().lower() not in ("x86_64", "amd64"):
            raise RuntimeError("vendored Steamworks SDK library supports Linux x86-64 only")
        wheel_platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
        return "py3", "none", wheel_platform


setup(cmdclass={"bdist_wheel": PlatformWheel})
