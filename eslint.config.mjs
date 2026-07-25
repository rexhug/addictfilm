import globals from "globals";

export default [
  {
    ignores: ["frontend/e2e/**"],
  },
  {
    files: ["frontend/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: {
        ...globals.browser,
        showStats: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unreachable": "error",
      "no-constant-binary-expression": "error"
    }
  }
];
