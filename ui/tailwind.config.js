// Build the web UI stylesheet (output is committed, so installs need no build step):
//   tailwindcss -c ui/tailwind.config.js -i ui/input.css -o app/static/tailwind.css --minify
// Get the standalone CLI from https://github.com/tailwindlabs/tailwindcss/releases (v3.4).
module.exports = {
  content: ["./app/templates/**/*.html", "./app/static/site.js"],
  // Neutrals are CSS variables so ui/input.css can swap them for the light theme.
  theme: {
    extend: {
      colors: {
        white: "rgb(var(--white) / <alpha-value>)",
        zinc: Object.fromEntries(
          [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((n) => [n, `rgb(var(--zinc-${n}) / <alpha-value>)`])
        ),
      },
    },
  },
};
