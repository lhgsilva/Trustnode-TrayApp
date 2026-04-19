export default [
  {
    files: ["**/*.jsx"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } }
    },
    rules: {
      "no-use-before-define": ["error", { functions: false, classes: true, variables: true }]
    }
  }
];
