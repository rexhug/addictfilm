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
      "no-constant-binary-expression": "error",
      // Ловят классы ошибок, а не форматирование: неявное приведение типов,
      // затенение переменных и обращение до объявления уже приводили к багам.
      "eqeqeq": ["error", "smart"],
      "no-unused-vars": ["error", { "args": "none", "varsIgnorePattern": "^_", "caughtErrors": "none" }],
      "no-shadow": "error",
      // variables:false — приложение это один классический скрипт, где хелперы
      // объявлены рядом со своим разделом, а не в начале файла. Все такие ссылки
      // срабатывают внутри функций, вызываемых уже после объявления; переносить
      // объявления в 3000-строчном файле рискованнее, чем польза от правила.
      "no-use-before-define": ["error", { "functions": false, "variables": false }],
      "no-promise-executor-return": "error"
    }
  }
];
