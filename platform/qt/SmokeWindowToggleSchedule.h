#ifndef SMOKEWINDOWTOGGLESCHEDULE_H
#define SMOKEWINDOWTOGGLESCHEDULE_H

// VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1: the schedule of window-state toggles the --gui-smoke-playback harness
// drives MID-PLAY (env MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES), so a venue leg can prove that a fullscreen toggle,
// a restore and a maximize during Play keep it playing (PLAYER-FULLSCREEN-KEEPS-PLAYING-1). Every #848 r2 leg entered
// full screen BEFORE its measured Play, so no resize ever happened with Play checked.
//
// Grammar: comma-separated `<kind>@<ms>`; kind in {fs_on, fs_off, restore, maximize}; ms a non-negative decimal
// integer (digits only, at most 9); strictly ascending, each at least kMinGapMs after the previous (the settle window
// kSettleMs plus a margin, so one toggle's end phase is logged before the next begins). No whitespace, no empty
// entry. Any malformed entry rejects the WHOLE schedule: the result is empty and carries a reason.
//
// Pure parse, no Qt GUI types. It drives no window and never touches Play.

#include <string>
#include <vector>

namespace smoke_window_toggle_schedule
{

constexpr int kSettleMs = 2000;
constexpr int kMinGapMs = 2500;

enum class Kind
{
    FsOn,
    FsOff,
    Restore,
    Maximize
};

inline const char * kindName( Kind kind )
{
    switch( kind )
    {
    case Kind::FsOn:     return "fs_on";
    case Kind::FsOff:    return "fs_off";
    case Kind::Restore:  return "restore";
    case Kind::Maximize: return "maximize";
    }
    return "unknown";
}

struct Entry
{
    Kind kind;
    int atMs;
};

struct Schedule
{
    std::vector<Entry> entries;   // empty when rejected
    std::string reason;           // "ok" when accepted, else why the whole schedule was rejected

    bool ok() const { return reason == "ok"; }
};

namespace detail
{
inline Schedule reject( const std::string & why )
{
    Schedule rejected;
    rejected.reason = why;
    return rejected;
}

inline bool kindFromName( const std::string & name, Kind * out )
{
    if( name == "fs_on" )     { *out = Kind::FsOn;     return true; }
    if( name == "fs_off" )    { *out = Kind::FsOff;    return true; }
    if( name == "restore" )   { *out = Kind::Restore;  return true; }
    if( name == "maximize" )  { *out = Kind::Maximize; return true; }
    return false;
}
} // namespace detail

inline Schedule parse( const std::string & text )
{
    if( text.empty() ) return detail::reject( "empty_schedule" );
    Schedule result;
    size_t start = 0;
    for( ;; )
    {
        const size_t comma = text.find( ',', start );
        const std::string item = text.substr( start, comma == std::string::npos ? std::string::npos : comma - start );
        if( item.empty() ) return detail::reject( "empty_entry" );
        const size_t at = item.find( '@' );
        if( at == std::string::npos ) return detail::reject( "missing_at_sign" );
        Kind kind = Kind::FsOn;
        if( !detail::kindFromName( item.substr( 0, at ), &kind ) ) return detail::reject( "unknown_kind" );
        const std::string msText = item.substr( at + 1 );
        if( msText.empty() || msText.size() > 9 ) return detail::reject( "bad_ms" );
        long long ms = 0;
        for( char c : msText )
        {
            if( c < '0' || c > '9' ) return detail::reject( "bad_ms" );
            ms = ms * 10 + ( c - '0' );
        }
        if( !result.entries.empty() )
        {
            const int previous = result.entries.back().atMs;
            if( ms <= previous ) return detail::reject( "not_ascending" );
            if( ms - previous < kMinGapMs ) return detail::reject( "gap_too_small" );
        }
        result.entries.push_back( Entry{ kind, static_cast<int>( ms ) } );
        if( comma == std::string::npos ) break;
        start = comma + 1;
    }
    result.reason = "ok";
    return result;
}

} // namespace smoke_window_toggle_schedule

#endif // SMOKEWINDOWTOGGLESCHEDULE_H
