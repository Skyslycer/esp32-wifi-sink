# Browser flasher dependencies

`esptool-0.7.1.js` is a locally bundled ES module containing Espressif esptool-js
0.7.1, SparkMD5 3.0.2 and their runtime dependencies. Associated licenses are
included alongside it. Nothing is loaded from a CDN at runtime.

To rebuild from the repository root:

```sh
cd tools/webflasher
npm ci
npm run build
```

The lock file pins the bundle inputs. Node/npm are build tools only; the deployed
Python server serves the generated module as a static asset.
