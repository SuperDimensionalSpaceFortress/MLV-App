/*!
 * \file PlaybackEnvFlags.h
 * \brief The exact values the PLAYBACK-STARTUP-WAIT-PREWARM-1 environment switches accept (pure, unit-tested).
 *
 * MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY (default off) and MLVAPP_PLAYBACK_ROUTE_PREWARM (default on) take, trimmed and
 * case-insensitive:
 *   on:  1 true on yes
 *   off: 0 false off no
 * Unset or empty keeps the default. Anything else ("1garbage", "trueish", "2") is invalid: it keeps the default and
 * the caller logs it once, so a typo never silently flips a switch either way.
 */

#ifndef PLAYBACKENVFLAGS_H
#define PLAYBACKENVFLAGS_H

#include <cctype>
#include <string>

namespace playback_env_flag
{

enum class Value { Unset, On, Off, Invalid };

inline Value parse( const char *raw )
{
    if( !raw ) return Value::Unset;
    std::string value( raw );
    const auto isSpace = []( char c ) { return std::isspace( static_cast<unsigned char>( c ) ) != 0; };
    while( !value.empty() && isSpace( value.back() ) ) value.pop_back();
    size_t first = 0;
    while( first < value.size() && isSpace( value[first] ) ) ++first;
    value = value.substr( first );
    for( char &c : value ) c = static_cast<char>( std::tolower( static_cast<unsigned char>( c ) ) );
    if( value.empty() ) return Value::Unset;
    if( value == "1" || value == "true" || value == "on" || value == "yes" ) return Value::On;
    if( value == "0" || value == "false" || value == "off" || value == "no" ) return Value::Off;
    return Value::Invalid;
}

inline bool resolve( Value value, bool defaultValue )
{
    if( value == Value::On ) return true;
    if( value == Value::Off ) return false;
    return defaultValue;
}

} // namespace playback_env_flag

#endif // PLAYBACKENVFLAGS_H
