# Shared by every board project before including ESP-IDF's project.cmake.
set(firmware_version_file "${CMAKE_CURRENT_LIST_DIR}/VERSION")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${firmware_version_file}")
file(READ "${firmware_version_file}" PROJECT_VER)
string(STRIP "${PROJECT_VER}" PROJECT_VER)
string(LENGTH "${PROJECT_VER}" firmware_version_length)
if(firmware_version_length LESS 1 OR firmware_version_length GREATER 31)
    message(FATAL_ERROR "firmware/VERSION must contain a version of 1 to 31 characters")
endif()
