```markdown
# ai-sessions-render Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill introduces the core development patterns and conventions used in the `ai-sessions-render` TypeScript codebase. It covers file naming, import/export styles, commit message conventions, and testing patterns. By following these guidelines, contributors can ensure consistency and maintainability across the project.

## Coding Conventions

### File Naming
- Use **camelCase** for all file names.
  - Example:  
    ```
    sessionRenderer.ts
    userSessionManager.test.ts
    ```

### Import Style
- Use **relative imports** for referencing modules.
  - Example:
    ```typescript
    import { renderSession } from './sessionRenderer';
    ```

### Export Style
- Use **named exports** for all modules.
  - Example:
    ```typescript
    // sessionRenderer.ts
    export function renderSession(session: Session) { ... }
    ```

### Commit Messages
- Follow **conventional commit** format.
- Common prefix: `test`
- Example:
  ```
  test: add edge case for session timeout handling
  ```

## Workflows

### Testing Code
**Trigger:** When you need to run or add tests  
**Command:** `/run-tests`

1. Identify or create a test file using the `*.test.*` pattern (e.g., `userSessionManager.test.ts`).
2. Write tests using the project's preferred testing framework (framework not specified; check existing test files for style).
3. Run the test suite using the project's configured test runner (refer to project documentation or package scripts).
4. Ensure all tests pass before committing changes.

### Adding a New Module
**Trigger:** When you need to add new functionality  
**Command:** `/add-module`

1. Create a new TypeScript file using camelCase naming (e.g., `newFeature.ts`).
2. Use named exports for all functions or constants.
3. Import dependencies using relative paths.
4. Add corresponding test file with the `.test.ts` suffix.
5. Write tests following the established patterns.
6. Commit with a conventional commit message.

## Testing Patterns

- Test files use the `*.test.*` naming convention (e.g., `sessionRenderer.test.ts`).
- The testing framework is not explicitly specified; review existing test files to match the style.
- Place tests alongside or near the modules they test.
- Example test file structure:
  ```typescript
  // sessionRenderer.test.ts
  import { renderSession } from './sessionRenderer';

  describe('renderSession', () => {
    it('should render a session correctly', () => {
      // test implementation
    });
  });
  ```

## Commands
| Command      | Purpose                                   |
|--------------|-------------------------------------------|
| /run-tests   | Run all test files in the codebase        |
| /add-module  | Scaffold a new module with tests          |
```
