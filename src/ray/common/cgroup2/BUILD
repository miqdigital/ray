load("//bazel:ray.bzl", "ray_cc_library")

ray_cc_library(
    name = "cgroup_driver_interface",
    hdrs = [
        "cgroup_driver_interface.h",
    ],
    deps = [
        "//src/ray/common:status",
        "//src/ray/common:status_or",
    ],
)

ray_cc_library(
    name = "sysfs_cgroup_driver",
    srcs = ["sysfs_cgroup_driver.cc"],
    hdrs = [
        "sysfs_cgroup_driver.h",
    ],
    deps = [
        ":cgroup_driver_interface",
        "//src/ray/common:status",
        "//src/ray/common:status_or",
        "//src/ray/util:logging",
        "@com_google_absl//absl/strings",
    ],
)
