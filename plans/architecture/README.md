# Architecture Enforcement

This directory contains architecture enforcement rules for **windbreak**.

## Purpose

Architecture rules ensure:
- **Layer Separation**: Higher layers depend on lower layers only
- **No Circular Dependencies**: Prevent tight coupling
- **Domain Independence**: Domain logic remains pure and testable

## Tool: import-linter

### Installation

```bash
pip install import-linter
```

### Usage

Run the architecture checks:

```bash
./run-check.sh
```

Or manually:

```bash
lint-imports --config .importlinter
```


## Rules Enforced

### Layer Separation

- **Presentation** → Application → Domain → Infrastructure
- Domain layer cannot depend on infrastructure or presentation
- Each layer can only depend on layers below it

### Circular Dependencies

All circular dependencies are forbidden. They create:
- Tight coupling
- Difficult testing
- Complex refactoring
- Hidden dependencies

### Domain Independence

The domain layer must remain pure:
- No framework dependencies
- No database dependencies
- No UI dependencies
- Only business logic

## Customization

Edit the configuration file:
- Python: `.importlinter`
- TypeScript: `.dependency-cruiser.js`
- Go: `.go-arch-lint.yml`
- Rust: `deny.toml`
- Swift: `.swiftlint-architecture.yml`
- Kotlin: `ArchitectureTest.kt`
- C/C++: `check_architecture.py` (the ALLOWED_DEPENDENCIES matrix at the top)
- Java: `ArchitectureTest.java` (the layered-architecture rules in the test)
- C#: `ArchitectureTest.cs` (the NetArchTest rules in the test)
- Ruby: `packwerk.yml` / `package.yml` (the Packwerk package boundaries)

See documentation:
- Python: https://import-linter.readthedocs.io/
- TypeScript: https://github.com/sverweij/dependency-cruiser
- Go: https://github.com/fe3dback/go-arch-lint
- Rust: https://embarkstudios.github.io/cargo-deny/
- Swift: https://realm.github.io/SwiftLint/custom_rules.html
- Kotlin: https://docs.konsist.lemonappdev.com/
- C/C++: the header comment in check_architecture.py (self-documented)
- Java: https://www.archunit.org/
- C#: https://github.com/BenMorris/NetArchTest
- Ruby: https://github.com/Shopify/packwerk

## Integration

Add to CI pipeline:

```yaml
- name: Check Architecture
  run: ./plans/architecture/run-check.sh
```

## References

- Clean Architecture (Robert C. Martin)
- Hexagonal Architecture (Alistair Cockburn)
- Domain-Driven Design (Eric Evans)
