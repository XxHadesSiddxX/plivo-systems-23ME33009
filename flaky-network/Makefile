CC ?= cc
CFLAGS ?= -O2 -Wall

all: sender receiver

sender: sender.c rs.h
	$(CC) $(CFLAGS) -o sender sender.c

receiver: receiver.c rs.h
	$(CC) $(CFLAGS) -o receiver receiver.c

clean:
	rm -f sender receiver
