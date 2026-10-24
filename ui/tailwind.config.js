// Build the web UI stylesheet (output is committed, so installs need no build step):
//   tailwindcss -c ui/tailwind.config.js -i ui/input.css -o app/static/tailwind.css --minify
// Get the standalone CLI from https://github.com/tailwindlabs/tailwindcss/releases (v3.4).
module.exports = {
  content: ["./app/templates/**/*.html", "./app/static/site.js"],
  theme: { extend: {} },
};
