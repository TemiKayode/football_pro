const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const pub = path.join(root, "public");
fs.mkdirSync(pub, { recursive: true });
console.log("Public assets ready.");
