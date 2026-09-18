/*
 * A stand-in for the ghcplatform.h that GHC ships in its RTS include
 * directory, for the benchmark corpus only.
 *
 * Describes the same fixed x86_64 Linux platform as macros.tsv, so a module
 * that includes this header sees the platform it would be built for. Written
 * from the macro names the real header defines, not copied from it.
 */

#pragma once

#define GHC_STAGE 2

#define BuildPlatform_TYPE x86_64_unknown_linux
#define HostPlatform_TYPE x86_64_unknown_linux

#define x86_64_unknown_linux_BUILD 1
#define x86_64_unknown_linux_HOST 1

#define x86_64_BUILD_ARCH 1
#define x86_64_HOST_ARCH 1
#define BUILD_ARCH "x86_64"
#define HOST_ARCH "x86_64"

#define linux_BUILD_OS 1
#define linux_HOST_OS 1
#define BUILD_OS "linux"
#define HOST_OS "linux"

#define unknown_BUILD_VENDOR 1
#define unknown_HOST_VENDOR 1
#define BUILD_VENDOR "unknown"
#define HOST_VENDOR "unknown"

#define TABLES_NEXT_TO_CODE 1
