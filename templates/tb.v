`timescale 1ns/1ps
// Banco de pruebas: con N = 2 el contador va de 0 a 3 y el LED sigue su bit más significativo.
module tb;
    reg clk = 0;
    wire led;

    @@NAME@@ #(.N(2)) uut (.clk(clk), .led(led));

    always #10 clk = ~clk;      // 50 MHz

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb);
        #2000 $finish;
    end
endmodule
