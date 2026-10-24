# Vendored front-end

These files are copied in, not installed: the app has no build step and no
package manager, so a library's minified build ships as it comes from upstream.
Nothing in the repo recorded where they came from, which version they are, or
their licence, and the Geoman bundle carries no version banner at all.

Every file here is byte-identical to the upstream release named beside it.
`scripts/check-vendor-hashes.py` recomputes these sha256 values; `just ci` and
the lint workflow run it, so a file that changes without its record is refused.

Update procedure: download the new release, replace the files, put the new
version and hashes in this file, and re-run the check. Never edit a vendored
file by hand.

## Leaflet 1.9.4

- Upstream: <https://github.com/Leaflet/Leaflet/releases/tag/v1.9.4>
- Files taken from `https://unpkg.com/leaflet@1.9.4/dist/`
- Licence: BSD-2-Clause, © 2010-2023 Volodymyr Agafonkin, © 2010-2011 CloudMade
  (the banner in `leaflet.js` carries the same terms)

```
db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a  leaflet/leaflet.js
a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6  leaflet/leaflet.css
1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6  leaflet/images/layers.png
066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf  leaflet/images/layers-2x.png
574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437  leaflet/images/marker-icon.png
00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528  leaflet/images/marker-icon-2x.png
264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da  leaflet/images/marker-shadow.png
```

## Leaflet-Geoman Free 2.20.0

- Upstream: <https://github.com/geoman-io/leaflet-geoman/releases/tag/2.20.0>
- Files taken from `https://unpkg.com/@geoman-io/leaflet-geoman-free@2.20.0/dist/`
- Licence: MIT, © Geoman
- The npm package is `@geoman-io/leaflet-geoman-free`; `leaflet-geoman` is not
  published under that name, so `unpkg.com/leaflet-geoman` answers "package not
  found" rather than a version.
- 2.20.0 is identified by this file's hash matching that release, not by a
  banner: the minified bundle states no version.

```
9d903788bf18e9c78f2577f1cb4594a545d7cca8fcbdc4f009893186a2929b8c  leaflet-geoman/leaflet-geoman.min.js
a0df8e0f6301f9f71d772b2c61913bb9094ae0735fd2223ccd1aecd871ea250c  leaflet-geoman/leaflet-geoman.css
```
