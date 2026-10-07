# Atajos para trabajar sin la herramienta `fpga` (la programación se hace SOLO con `fpga prog`,
# que es la que avisa y lleva la cuenta de cargas de la flash).
NAME = @@NAME@@
SRC  = $(filter-out tb%.v, $(wildcard *.v))
TB  ?= @@TB@@

sim:
	iverilog -g2012 -o sim_$(basename $(TB)).vvp $(TB) $(SRC) && vvp sim_$(basename $(TB)).vvp

wave: sim
	gtkwave $$(ls -t *.vcd | head -1)

project:
	quartus_sh -t $(NAME).tcl

build: project
	quartus_sh --flow compile $(NAME)

clean:
	rm -rf db incremental_db output_files qdb greybox_tmp *.vvp *.vcd *.qpf *.qsf *.qws

.PHONY: sim wave project build clean
