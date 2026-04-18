const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const pub = path.join(root, "public");
const src = path.join(root, "templates", "index.html");
const dest = path.join(pub, "index.html");

fs.mkdirSync(pub, { recursive: true });
fs.copyFileSync(src, dest);
console.log("Synced templates/index.html -> public/index.html");
