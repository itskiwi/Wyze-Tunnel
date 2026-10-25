// Sunrise and sunset without the network, for the "Follow the sun" theme. Times are Unix seconds.

// The sunrise equation (NOAA's simple form, about a minute off). Polar day or night clamps to always light or dark.
function sunTimes(date, lat, lon) {
    const rad = Math.PI / 180, sin = (d) => Math.sin(d * rad);
    const d = date.getTime() / 864e5 - 10957.5;  // days since J2000 (2000-01-01 12:00 UTC)
    const noon = Math.round(d + lon / 360) - lon / 360;  // the solar noon closest to date
    const m = (357.5291 + 0.98560028 * noon) % 360;
    const l = (m + 1.9148 * sin(m) + 0.02 * sin(2 * m) + 0.0003 * sin(3 * m) + 282.9372) % 360;
    const transit = noon + 0.0053 * sin(m) - 0.0069 * sin(2 * l);
    const dec = Math.asin(sin(l) * sin(23.4397));
    const cosW = (sin(-0.833) - sin(lat) * Math.sin(dec)) / (Math.cos(lat * rad) * Math.cos(dec));
    const half = Math.acos(Math.min(1, Math.max(-1, cosW))) / rad / 360;
    const unix = (j) => Math.round((j + 10957.5) * 86400);
    return { sunrise: unix(transit - half), sunset: unix(transit + half) };
}

// Light between sunrise and sunset; golden in the 45 minutes after sunrise and the hour before sunset.
function sunPhase(now, sunrise, sunset) {
    const light = now >= sunrise && now < sunset;
    return { light, golden: light && (now < sunrise + 2700 || now >= sunset - 3600) };
}

if (typeof module !== "undefined") module.exports = { sunTimes, sunPhase };
