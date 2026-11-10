#pragma once

#if __has_include("link_config.local.h")
#include "link_config.local.h"
#endif

#ifndef LINK_SENSOR_MAC
#define LINK_SENSOR_MAC ""
#endif
#ifndef LINK_GATEWAY_MAC
#define LINK_GATEWAY_MAC ""
#endif
#ifndef LINK_PMK
#define LINK_PMK ""
#endif
#ifndef LINK_LMK
#define LINK_LMK ""
#endif

namespace LinkConfig {
  constexpr unsigned char CHANNEL   = 1;
  constexpr const char* SENSOR_MAC  = LINK_SENSOR_MAC;
  constexpr const char* GATEWAY_MAC = LINK_GATEWAY_MAC;
  constexpr const char* PMK         = LINK_PMK;
  constexpr const char* LMK         = LINK_LMK;
}  // namespace LinkConfig
