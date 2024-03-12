/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#version 450 core
// clang-format off
#define PRECISION ${PRECISION}
// clang-format on

#include "indexing_utils.h"

layout(std430) buffer;

// clang-format off
layout(set = 0, binding = 0, ${IMAGE_FORMAT[DTYPE]}) uniform PRECISION restrict writeonly ${IMAGE_T[NDIM][DTYPE]} image_out;
// clang-format on
layout(set = 0, binding = 1) buffer  PRECISION restrict readonly Buffer {
  ${T[DTYPE]} data[];
}
buffer_in;

layout(set = 0, binding = 2) uniform PRECISION restrict GpuSizes {
  ivec4 data;
}
gpu_sizes;

layout(set = 0, binding = 3) uniform PRECISION restrict CpuSizes {
  ivec4 data;
}
cpu_sizes;

layout(local_size_x_id = 0, local_size_y_id = 1, local_size_z_id = 2) in;

void main() {
  const ivec3 pos = ivec3(gl_GlobalInvocationID);
  const ivec4 coord = POS_TO_COORD_${PACKING}(pos, gpu_sizes.data);

  if (any(greaterThanEqual(coord, gpu_sizes.data))) {
    return;
  }

  const int base_index = COORD_TO_BUFFER_IDX(coord, cpu_sizes.data);
  const ivec4 buf_indices =
      base_index + ivec4(0, 1, 2, 3) * (gpu_sizes.data.x * gpu_sizes.data.y);

  ${T[DTYPE]} val_x = buffer_in.data[buf_indices.x];
  ${T[DTYPE]} val_y = buffer_in.data[buf_indices.y];
  ${T[DTYPE]} val_z = buffer_in.data[buf_indices.z];
  ${T[DTYPE]} val_w = buffer_in.data[buf_indices.w];

  ${VEC4_T[DTYPE]} texel = ${VEC4_T[DTYPE]}(val_x, val_y, val_z, val_w);

  if (coord.z + 3 >= cpu_sizes.data.z) {
    ivec4 c_ind = ivec4(coord.z) + ivec4(0, 1, 2, 3);
    vec4 valid_c = vec4(lessThan(c_ind, ivec4(cpu_sizes.data.z)));
    texel = texel * valid_c;
  }

  imageStore(image_out, ${GET_POS[NDIM]("pos")}, texel);
}
