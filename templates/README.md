# @@NAME@@

Trabajo para la tarjeta **@@BOARDNAME@@** (`@@DEVICE@@`), creado con `fpga-cli`.

| Carpeta o archivo | Para qué |
|---|---|
| `src/` | Los módulos y el top en Verilog |
| `tb/` | Los testbenches (cada uno genera su forma de onda `.vcd`) |
| `sim/` | Archivos de simulación (`.vvp`); se regeneran |
| `waves/` | Formas de onda (`.vcd`) para GTKWave; se regeneran |
| `build/` | Lo de Quartus: el `.tcl` y el `.sdc` (generados desde `fpga.toml`), el proyecto, los reportes y los **archivos de carga** en `build/output_files/` (`.sof`, `.pof`, `.jic`) |
| `fpga.toml` | Tarjeta, dispositivo, archivos y pines de este trabajo |

## Flujo

```
fpga sim [tb]  # iverilog + vvp (con varios testbenches, indica cuál)
fpga wave [tb] # gtkwave
fpga build     # proyecto + compilación con Quartus (sin abrir la GUI)
fpga prog      # programar la tarjeta
```

Para crecer el proyecto:

```
fpga add contador          # nuevo módulo contador.v (se suma a las fuentes)
fpga new-tb contador       # testbench tb_contador.v con sus entradas y salidas ya conectadas
fpga pin led[3:0] led3 led2 led1 led0     # asignar puertos del top a señales de la tarjeta
fpga pin                   # ver las asignaciones actuales
```

Sin la herramienta: `make sim`, `make build` y, con Quartus, `quartus_pgm` a mano.

@@NOTE@@
## Pines usados

@@PINS@@
